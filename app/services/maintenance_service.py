"""Mantenimiento: depura lo que vence (sesiones, retos, huellas de capturas, QR, cuentas recordadas,
contadores del límite de peticiones, muestras faciales aprendidas que dejaron de servir, errores
solucionados viejos) FUERA de las peticiones de los usuarios.

Antes cada petición borraba "de paso" lo vencido de todas las empresas: con mucha carga varias
peticiones competían por las mismas filas y se formaban filas de espera. Ahora:

- Una tarea en segundo plano lo ejecuta cada MAINTENANCE_INTERVAL_SECONDS en cada instancia, pero
  solo una a la vez trabaja (candado `pg_try_advisory_lock`); las demás se saltan la vuelta.
- Borra en lotes de MAINTENANCE_BATCH_SIZE filas, cada lote en su propia transacción corta.
- `python -m app.cli purge` hace lo mismo bajo demanda (o desde un cron externo).

Los datos vencidos ya no sirven aunque sigan en la tabla (cada consulta valida su vigencia): la
depuración solo libera espacio.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.models import (
    AssistantQuery,
    AuthSession,
    CaptureFingerprint,
    EmployeeQr,
    ErrorOccurrence,
    ErrorReport,
    ErrorStatus,
    FaceChallenge,
    FaceEmbedding,
    RateLimitCounter,
    RememberedAccount,
)
from app.models.face_embedding import last_useful
from app.repositories.maintenance_repository import delete_batch

logger = logging.getLogger(__name__)

#: Candado de PostgreSQL que asegura una sola depuración a la vez entre instancias.
MAINTENANCE_LOCK_KEY = 7_420_032
#: Tope de lotes por tabla en una vuelta (el resto sigue en la siguiente).
MAX_BATCHES_PER_TABLE = 200


@dataclass(frozen=True)
class Purge:
    name: str
    key: InstrumentedAttribute[Any]
    condition: Callable[[datetime], ColumnElement[bool]]


#: Qué se depura y desde cuándo (las retenciones viven en la configuración).
PURGES: tuple[Purge, ...] = (
    Purge("sesiones", AuthSession.id, lambda now: AuthSession.expires_at < now - timedelta(days=1)),
    Purge("retos de prueba de vida", FaceChallenge.id, lambda now: FaceChallenge.expires_at <= now),
    Purge(
        "huellas de capturas",
        CaptureFingerprint.digest,
        lambda now: CaptureFingerprint.created_at < now - timedelta(days=settings.FACE_REPLAY_RETENTION_DAYS),
    ),
    Purge(
        "códigos QR",
        EmployeeQr.id,
        lambda now: EmployeeQr.expires_at < now - timedelta(days=settings.QR_TOKEN_RETENTION_DAYS),
    ),
    Purge("cuentas recordadas", RememberedAccount.id, lambda now: RememberedAccount.expires_at <= now),
    # Evolución del reconocimiento facial: lo aprendido que dejó de servir deja su lugar (face_learning).
    Purge(
        "muestras faciales aprendidas sin uso",
        FaceEmbedding.id,
        lambda now: and_(
            FaceEmbedding.learned, last_useful() < now - timedelta(days=settings.FACE_LEARNING_STALE_DAYS)
        ),
    ),
    Purge(
        "ocurrencias de errores",
        ErrorOccurrence.id,
        lambda now: ErrorOccurrence.occurred_at < now - timedelta(days=settings.ERROR_OCCURRENCE_RETENTION_DAYS),
    ),
    Purge(
        "errores solucionados",
        ErrorReport.id,
        lambda now: and_(
            ErrorReport.status == ErrorStatus.RESOLVED,
            ErrorReport.last_seen_at < now - timedelta(days=settings.ERROR_RESOLVED_RETENTION_DAYS),
        ),
    ),
    Purge(
        "preguntas al asistente de reportes",
        AssistantQuery.id,
        lambda now: AssistantQuery.created_at < now - timedelta(days=settings.REPORT_QUERY_RETENTION_DAYS),
    ),
    Purge(
        "límites de peticiones",
        RateLimitCounter.key,
        lambda now: RateLimitCounter.expires_at < now - timedelta(minutes=5),
    ),
)


def purge_expired(db: Session, *, batch_size: int | None = None, now: datetime | None = None) -> dict[str, int]:
    """Depura todo lo vencido en lotes; devuelve cuántas filas salieron de cada tabla."""
    size = batch_size or settings.MAINTENANCE_BATCH_SIZE
    moment = now or datetime.now(UTC)
    removed: dict[str, int] = {}
    for purge in PURGES:
        total = 0
        try:
            for _ in range(MAX_BATCHES_PER_TABLE):
                count = delete_batch(db, purge.key, purge.condition(moment), size)
                db.commit()
                total += count
                if count < size:
                    break
        except SQLAlchemyError:
            # Cada tabla por separado: una que falla (bloqueo, statement_timeout) no deja sin depurar a
            # las demás; lo borrado en lotes anteriores ya quedó confirmado y la siguiente vuelta sigue.
            db.rollback()
            logger.exception("Falló la depuración de %s (se reintenta en la siguiente vuelta)", purge.name)
        removed[purge.name] = total
    return removed


def run_once() -> dict[str, int] | None:
    """Una vuelta de mantenimiento si ninguna otra instancia la está haciendo (None si se saltó)."""
    if engine.dialect.name != "postgresql":
        with SessionLocal() as db:
            return purge_expired(db)
    with engine.connect() as lock:
        if not lock.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": MAINTENANCE_LOCK_KEY}).scalar():
            return None
        try:
            with SessionLocal() as db:
                return purge_expired(db)
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MAINTENANCE_LOCK_KEY})


class MaintenanceScheduler:
    """Hilo en segundo plano que ejecuta `run_once` cada `interval` segundos (se detiene al apagar)."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="maintenance", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                removed = run_once()
            except Exception:  # la BD puede estar caída: se reintenta en la siguiente vuelta
                logger.exception("Falló el mantenimiento programado")
                continue
            if removed and any(removed.values()):
                logger.info("Mantenimiento: %s", ", ".join(f"{k}={v}" for k, v in removed.items() if v))
