"""Vigilancia de la recuperación a un punto en el tiempo (PITR) desde el servicio `backup` (README "Respaldos y
restauración" → "Recuperación a un punto en el tiempo"; regla 7: ninguna falla de una dependencia pasa en silencio).

El archivo continuo del WAL corre DENTRO de PostgreSQL (servicio `db`, `postgres/pgbackrest.sh`) y los respaldos base en
el servicio `pitr`: ninguno de los dos es Python ni escribe en la base. Este monitor es quien avisa al ADMIN. Cada
`PITR_CHECK_SECONDS` junta tres fuentes:

1. **PostgreSQL** (`archive_repository`, con el dueño por la conexión directa): `archive_mode`, el último segmento
   archivado y la última falla (`pg_stat_archiver`), cuántos segmentos esperan subir y desde cuándo el más viejo (el
   *lag*), y lo que ocupa `pg_wal`.
2. **El servicio pitr**: su estado en el volumen compartido (`scheduler.json`): último respaldo base completo y
   diferencial, su error y si sigue reportando.
3. **El guardián del disco**: `wal-dropped.json`, que escribe el archive_command cuando pgBackRest DESCARTA WAL al
   pasar de `PITR_WAL_MAX_MB` (el bucket no respondió a tiempo).

Cada problema es un aviso con una llave estable y su propio logger (`app.pitr.<llave>`): `logger.error` lo lleva a
"Errores del sistema" (`ErrorLogHandler`: una fila por llave, con su contador, que se reabre sola si vuelve después de
resolverla). Mientras sigue activo se repite cada `PITR_ALERT_REPEAT_MINUTES` (no en cada vuelta: señal, no ruido) y
al normalizarse queda una línea INFO. El monitor nunca lanza: una falla al revisar es, ella misma, un aviso.
Los datos se muestran en MB con dos decimales (regla 17).
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from app.core.config import settings
from app.core.database import build_engine
from app.repositories.archive_repository import archive_stats

logger = logging.getLogger(__name__)

#: Archivos del volumen compartido (los escriben postgres/pgbackrest.sh: `save_status` y `mark_dropped`).
SCHEDULER_FILE = "scheduler.json"
DROPPED_FILE = "wal-dropped.json"
#: Vueltas del servicio pitr sin reportar antes de darlo por detenido (tolera una vuelta lenta).
_MISSED_ROUNDS = 3


@dataclass(frozen=True)
class ArchiveState:
    """Lo que PostgreSQL dice de su archivador en este momento."""

    archive_mode: str
    archived_count: int
    last_archived_wal: str | None
    last_archived_at: datetime | None
    failed_count: int
    last_failed_wal: str | None
    last_failed_at: datetime | None
    ready_segments: int
    oldest_ready_at: datetime | None
    segment_bytes: int
    wal_bytes: int
    checked_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> ArchiveState:
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})

    @property
    def backlog_bytes(self) -> int:
        """WAL que espera subir (lo que crece en el disco si el bucket no responde)."""
        return self.ready_segments * self.segment_bytes

    @property
    def lag_seconds(self) -> float:
        """Cuánto lleva esperando el segmento más viejo (0 = nada espera)."""
        return (self.checked_at - self.oldest_ready_at).total_seconds() if self.oldest_ready_at else 0.0

    @property
    def failing(self) -> bool:
        """El último intento de archivar falló (después del último éxito)."""
        if self.last_failed_at is None:
            return False
        return self.last_archived_at is None or self.last_failed_at > self.last_archived_at


def _at(value: Any) -> datetime:
    """Segundos de la época (como los escribe `date +%s`) a un instante en UTC."""
    return datetime.fromtimestamp(float(value), UTC)


def _moment(value: Any) -> datetime | None:
    return None if value is None else _at(value)


@dataclass(frozen=True)
class SchedulerState:
    """Lo que reporta el servicio pitr en cada vuelta (`postgres/pgbackrest.sh`, `save_status`)."""

    updated_at: datetime
    state: str
    last_error: str = ""
    last_error_at: datetime | None = None
    last_full_at: datetime | None = None
    last_backup_at: datetime | None = None
    last_backup_type: str = ""
    oldest_backup_at: datetime | None = None
    backups: int = 0
    repo_bytes: int = 0
    running_since: datetime | None = None
    running_type: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SchedulerState:
        moments = ("last_error_at", "last_full_at", "last_backup_at", "oldest_backup_at", "running_since")
        return cls(
            updated_at=_at(data["updated_at"]),
            state=str(data["state"]),
            last_error=str(data.get("last_error") or ""),
            last_backup_type=str(data.get("last_backup_type") or ""),
            backups=int(data.get("backups") or 0),
            repo_bytes=int(data.get("repo_bytes") or 0),
            running_type=str(data.get("running_type") or ""),
            **{name: _moment(data.get(name)) for name in moments},
        )


@dataclass(frozen=True)
class Dropped:
    """El guardián descartó WAL (desde `first`, la última vez en `last`)."""

    first: datetime
    last: datetime
    count: int
    last_wal: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Dropped:
        return cls(_at(data["first"]), _at(data["last"]), int(data["count"]), str(data.get("last_wal") or ""))


@dataclass(frozen=True)
class Snapshot:
    """Las tres fuentes de una revisión; lo que no se pudo leer trae su motivo."""

    archive: ArchiveState | None = None
    archive_error: str | None = None
    scheduler: SchedulerState | None = None
    scheduler_error: str | None = None
    dropped: Dropped | None = None
    dropped_error: str | None = None


@dataclass(frozen=True)
class Alert:
    """Un problema para el ADMIN: llave estable (su fila en "Errores del sistema") y el detalle de este momento."""

    key: str
    message: str


def _mb(value: float) -> str:
    return f"{value / 1_048_576:,.2f} MB"


def _when(moment: datetime | None) -> str:
    return "nunca" if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ago(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds < 7200 else f"{seconds / 3600:.1f} h"


def _archive_alerts(archive: ArchiveState) -> list[Alert]:
    if archive.archive_mode != "on":
        return [
            Alert(
                "archive_off",
                f"PITR_ENABLED=true pero PostgreSQL no archiva el WAL (archive_mode={archive.archive_mode}): el "
                "servicio db corre sin pgBackRest o sin su configuración (revisa su log y recréalo: docker compose "
                "up -d db). Sin esto no hay recuperación a un punto en el tiempo.",
            )
        ]
    alerts: list[Alert] = []
    guard = settings.PITR_WAL_MAX_MB * 1_048_576
    waiting = (
        f"{archive.ready_segments} segmentos ({_mb(archive.backlog_bytes)}, "
        f"{archive.backlog_bytes * 100 / guard:.0f} % del guardián de {_mb(guard)}) esperan en pg_wal"
    )
    if archive.lag_seconds >= settings.PITR_LAG_ALERT_SECONDS:
        failure = (
            f" Última falla: {archive.last_failed_wal} el {_when(archive.last_failed_at)}." if archive.failing else ""
        )
        alerts.append(
            Alert(
                "archive_lag",
                f"El WAL no está saliendo al bucket: el segmento más viejo lleva {_ago(archive.lag_seconds)} esperando "
                f"y {waiting}. Último archivado: {archive.last_archived_wal or 'ninguno'} el "
                f"{_when(archive.last_archived_at)}.{failure} Mientras tanto, lo nuevo no se puede recuperar si se "
                "pierde el equipo.",
            )
        )
    if archive.backlog_bytes >= settings.PITR_WAL_ALERT_MB * 1_048_576:
        alerts.append(
            Alert(
                "wal_backlog",
                f"WAL acumulado sin subir: {waiting}. Al llegar al 100 % se descarta para proteger el disco y la "
                "recuperación continua se interrumpe hasta el siguiente respaldo base completo.",
            )
        )
    return alerts


def _scheduler_stalled(scheduler: SchedulerState, now: datetime) -> Alert | None:
    allowed = timedelta(seconds=settings.PITR_CHECK_SECONDS * _MISSED_ROUNDS)
    since = scheduler.updated_at
    if scheduler.state == "running" and scheduler.running_since is not None:
        since, allowed = scheduler.running_since, allowed + timedelta(seconds=settings.PITR_BACKUP_TIMEOUT_SECONDS)
    if now - since <= allowed:
        return None
    return Alert(
        "scheduler_stalled",
        f"El servicio pitr dejó de reportar desde {_when(scheduler.updated_at)} (estado: {scheduler.state}): ¿se "
        "detuvo? (docker compose ps pitr). Sin él no hay respaldos base nuevos.",
    )


def _scheduler_alerts(scheduler: SchedulerState, now: datetime) -> list[Alert]:
    if scheduler.state == "disabled":
        return [
            Alert(
                "scheduler_disabled",
                "El servicio pitr corre con PITR_ENABLED=false: recréalo para que tome el .env (docker compose up -d "
                "pitr). Sin él no hay respaldos base.",
            )
        ]
    alerts = [stalled] if (stalled := _scheduler_stalled(scheduler, now)) else []
    if scheduler.last_error and scheduler.last_error_at is not None:
        alerts.append(
            Alert(
                "base_backup_failed",
                f"Falló el servicio pitr el {_when(scheduler.last_error_at)} (se reintenta cada "
                f"{settings.PITR_RETRY_MINUTES} min): {scheduler.last_error}",
            )
        )
    every = settings.PITR_DIFF_BACKUP_HOURS or settings.PITR_FULL_BACKUP_HOURS
    expected = timedelta(hours=every, seconds=settings.PITR_BACKUP_TIMEOUT_SECONDS)
    if scheduler.last_backup_at is not None and now - scheduler.last_backup_at > expected:
        alerts.append(
            Alert(
                "base_backup_stale",
                f"El último respaldo base es del {_when(scheduler.last_backup_at)} (se esperan cada {every} h): "
                "restaurar desde uno viejo reproduce más WAL y tarda más.",
            )
        )
    return alerts


def evaluate(snapshot: Snapshot, now: datetime, *, watching_since: datetime) -> list[Alert]:
    """Los avisos de una revisión (función pura: cada regla con su prueba)."""
    alerts: list[Alert] = []
    if snapshot.archive_error:
        alerts.append(Alert("archive_unreadable", f"No se pudo revisar el archivo del WAL: {snapshot.archive_error}"))
    elif snapshot.archive is not None:
        alerts += _archive_alerts(snapshot.archive)
    if snapshot.dropped_error or snapshot.dropped is not None:
        dropped = snapshot.dropped
        detail = (
            f"descartó {dropped.count} segmentos de WAL desde el {_when(dropped.first)} "
            f"(el último{f', {dropped.last_wal},' if dropped.last_wal else ''} el {_when(dropped.last)})"
            if dropped
            else f"descartó WAL (aviso ilegible: {snapshot.dropped_error})"
        )
        alerts.append(
            Alert(
                "wal_dropped",
                f"El guardián del disco {detail}: no se puede recuperar a un instante entre ese momento y el próximo "
                "respaldo base completo, que el servicio pitr toma solo en cuanto el bucket responda.",
            )
        )
    if snapshot.scheduler_error:
        alerts.append(
            Alert("scheduler_unreadable", f"El estado del servicio pitr es ilegible: {snapshot.scheduler_error}")
        )
    elif snapshot.scheduler is not None:
        alerts += _scheduler_alerts(snapshot.scheduler, now)
    elif now - watching_since > timedelta(seconds=settings.PITR_CHECK_SECONDS * _MISSED_ROUNDS):
        alerts.append(
            Alert(
                "scheduler_missing",
                f"El servicio pitr no ha escrito su estado en {settings.PITR_STATUS_DIR}: ¿está arriba y con el "
                "volumen pitr? (docker compose ps pitr). Sin él no hay respaldos base.",
            )
        )
    return alerts


def _read_json(path: Path) -> dict[str, Any] | None:
    """El JSON de un archivo del volumen compartido; None si no existe (lo demás, como error con su motivo)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("no es un objeto JSON")
    return data


def _parse[T](path: Path, build: Callable[[dict[str, Any]], T]) -> tuple[T | None, str | None]:
    try:
        data = _read_json(path)
        return (build(data) if data is not None else None), None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"{path.name}: {type(exc).__name__}: {exc}"[:300]


@dataclass
class PitrMonitor:
    """La revisión periódica del servicio backup. `engine`: conexión directa del dueño (se arma al primer uso)."""

    engine: Engine | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    monotonic: Callable[[], float] = time.monotonic
    #: Llave → cuándo se registró por última vez (para repetir solo cada PITR_ALERT_REPEAT_MINUTES).
    active: dict[str, float] = field(default_factory=dict)
    watching_since: datetime = field(default_factory=lambda: datetime.now(UTC))

    def snapshot(self) -> Snapshot:
        """Lee las tres fuentes; cada una que falla trae su motivo y no impide leer las demás."""
        archive = archive_error = None
        try:
            self.engine = self.engine or build_engine(settings.DATABASE_DIRECT_URL)
            with self.engine.connect() as conn:
                archive = ArchiveState.from_row(archive_stats(conn, settings.DB_STATEMENT_TIMEOUT_MS))
        except Exception as exc:  # BD caída, sin permisos o tiempo límite: el aviso lo dice
            archive_error = f"{type(exc).__name__}: {exc}"[:300]
        folder = Path(settings.PITR_STATUS_DIR)
        scheduler, scheduler_error = _parse(folder / SCHEDULER_FILE, SchedulerState.from_json)
        dropped, dropped_error = _parse(folder / DROPPED_FILE, Dropped.from_json)
        return Snapshot(archive, archive_error, scheduler, scheduler_error, dropped, dropped_error)

    def check(self) -> list[Alert]:
        """Una revisión: registra los avisos nuevos o que toca repetir y anota los que se normalizaron. Nunca lanza."""
        try:
            alerts = evaluate(self.snapshot(), self.clock(), watching_since=self.watching_since)
        except Exception:
            logger.exception("Falló la revisión de la recuperación a un punto en el tiempo (PITR)")
            return []
        now = self.monotonic()
        repeat = settings.PITR_ALERT_REPEAT_MINUTES * 60
        for alert in alerts:
            if now - self.active.get(alert.key, -repeat) >= repeat:
                logging.getLogger(f"app.pitr.{alert.key}").error("PITR: %s", alert.message)
                self.active[alert.key] = now
        current = {alert.key for alert in alerts}
        for key in sorted(set(self.active) - current):
            logger.info("PITR: se normalizó el aviso %s", key)
            del self.active[key]
        return alerts


def describe(snapshot: Snapshot) -> list[str]:
    """El estado para una persona (python -m app.cli db pitr-status)."""
    lines: list[str] = []
    if archive := snapshot.archive:
        lines += [
            f"archive_mode: {archive.archive_mode}",
            f"último archivado: {archive.last_archived_wal or 'ninguno'} el {_when(archive.last_archived_at)} "
            f"({archive.archived_count} en total)",
            f"última falla: {archive.last_failed_wal or 'ninguna'} el {_when(archive.last_failed_at)} "
            f"({archive.failed_count} en total)",
            f"esperando subir: {archive.ready_segments} segmentos ({_mb(archive.backlog_bytes)}), el más viejo hace "
            f"{_ago(archive.lag_seconds)}; pg_wal ocupa {_mb(archive.wal_bytes)}",
        ]
    else:
        lines.append(f"PostgreSQL: {snapshot.archive_error}")
    if scheduler := snapshot.scheduler:
        lines += [
            f"servicio pitr: {scheduler.state} (reportó el {_when(scheduler.updated_at)})",
            f"último completo: {_when(scheduler.last_full_at)}; último respaldo base: "
            f"{_when(scheduler.last_backup_at)} ({scheduler.last_backup_type or '-'}); {scheduler.backups} respaldos, "
            f"{_mb(scheduler.repo_bytes)} en el bucket; se puede volver hasta: {_when(scheduler.oldest_backup_at)}",
        ]
    else:
        lines.append(f"servicio pitr: {snapshot.scheduler_error or 'sin estado todavía'}")
    if snapshot.dropped:
        lines.append(
            f"WAL descartado por el guardián: {snapshot.dropped.count} segmentos desde {_when(snapshot.dropped.first)}"
        )
    return lines
