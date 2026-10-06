"""Recuperación a un punto en el tiempo (PITR): el monitor del servicio backup (`app/services/pitr_monitor.py`), su
consulta (`app/repositories/archive_repository.py`) y `python -m app.cli db pitr-status`.

El archivo continuo y los respaldos base (pgBackRest en los servicios db y pitr) se prueban de punta a punta con
`scripts/db_pitr_check.sh` (PostgreSQL real, bucket falso, restauración a un instante, el guardián del disco y este
monitor contra la base real). Aquí, cada regla de los avisos con datos simulados."""

import argparse
import json
import logging
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app import cli
from app.core.config import settings
from app.repositories import archive_repository
from app.services import pitr_monitor
from app.services.error_reporter import ErrorLogHandler, ErrorReporter
from app.services.pitr_monitor import (
    Alert,
    ArchiveState,
    Dropped,
    PitrMonitor,
    SchedulerState,
    Snapshot,
    describe,
    evaluate,
)
from tests.conftest import OWNER_URL, owner_engine

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SEGMENT = 16 * 1024 * 1024


def _archive(**changes) -> ArchiveState:
    base = {
        "archive_mode": "on",
        "archived_count": 100,
        "last_archived_wal": "00000001000000000000000A",
        "last_archived_at": NOW - timedelta(seconds=30),
        "failed_count": 0,
        "last_failed_wal": None,
        "last_failed_at": None,
        "ready_segments": 0,
        "oldest_ready_at": None,
        "segment_bytes": SEGMENT,
        "wal_bytes": 64 * SEGMENT,
        "checked_at": NOW,
    }
    return ArchiveState(**(base | changes))


def _scheduler(**changes) -> SchedulerState:
    base = {
        "updated_at": NOW - timedelta(seconds=10),
        "state": "ok",
        "last_full_at": NOW - timedelta(days=2),
        "last_backup_at": NOW - timedelta(hours=2),
        "last_backup_type": "diff",
        "oldest_backup_at": NOW - timedelta(days=14),
        "backups": 5,
        "repo_bytes": 300 * 1024 * 1024,
    }
    return SchedulerState(**(base | changes))


def _alerts(snapshot: Snapshot, *, watching_since: datetime = NOW) -> dict[str, str]:
    return {alert.key: alert.message for alert in evaluate(snapshot, NOW, watching_since=watching_since)}


def test_all_good_means_no_alerts():
    assert _alerts(Snapshot(archive=_archive(), scheduler=_scheduler())) == {}


def test_postgres_without_archiving_is_reported_alone():
    found = _alerts(Snapshot(archive=_archive(archive_mode="off", ready_segments=999), scheduler=_scheduler()))
    assert list(found) == ["archive_off"] and "archive_mode=off" in found["archive_off"]


def test_a_stuck_archive_reports_its_lag_backlog_and_last_failure():
    stuck = _archive(
        ready_segments=100,
        oldest_ready_at=NOW - timedelta(minutes=20),
        failed_count=7,
        last_failed_wal="00000001000000000000000B",
        last_failed_at=NOW - timedelta(seconds=5),
    )
    found = _alerts(Snapshot(archive=stuck, scheduler=_scheduler()))
    assert set(found) == {"archive_lag", "wal_backlog"}
    assert (
        "20 min esperando" in found["archive_lag"] and "Última falla: 00000001000000000000000B" in found["archive_lag"]
    )
    assert "100 segmentos (1,600.00 MB, 20 % del guardián de 8,192.00 MB)" in found["wal_backlog"]
    # Sin falla registrada (el archive_command se colgó) y nada archivado nunca: el aviso lo dice igual.
    hung = _archive(
        ready_segments=2, oldest_ready_at=NOW - timedelta(hours=3), last_archived_wal=None, last_archived_at=None
    )
    found = _alerts(Snapshot(archive=hung, scheduler=_scheduler()))
    assert (
        list(found) == ["archive_lag"]
        and "3.0 h" in found["archive_lag"]
        and "ninguno el nunca" in found["archive_lag"]
    )
    assert "Última falla" not in found["archive_lag"]


@pytest.mark.parametrize(
    ("archived", "failed", "failing"),
    [
        (NOW, None, False),
        (NOW, NOW + timedelta(seconds=1), True),
        (NOW, NOW - timedelta(seconds=1), False),
        (None, NOW, True),
    ],
    ids=["sin-fallas", "fallo-despues", "fallo-antes", "nunca-archivo"],
)
def test_failing_means_the_last_attempt_failed(archived, failed, failing):
    state = _archive(last_archived_at=archived, last_failed_at=failed)
    assert state.failing is failing and state.lag_seconds == 0 and state.backlog_bytes == 0


def test_dropped_wal_is_reported_until_a_full_backup_repairs_it():
    dropped = Dropped(NOW - timedelta(hours=1), NOW - timedelta(minutes=5), 512, "000000010000000000000099")
    found = _alerts(Snapshot(archive=_archive(), scheduler=_scheduler(), dropped=dropped))
    assert list(found) == ["wal_dropped"] and "descartó 512 segmentos" in found["wal_dropped"]
    found = _alerts(Snapshot(archive=_archive(), scheduler=_scheduler(), dropped_error="wal-dropped.json: KeyError"))
    assert "aviso ilegible: wal-dropped.json: KeyError" in found["wal_dropped"]


def test_each_scheduler_problem_has_its_own_alert(monkeypatch):
    monkeypatch.setattr(settings, "PITR_CHECK_SECONDS", 60)
    monkeypatch.setattr(settings, "PITR_BACKUP_TIMEOUT_SECONDS", 3600)
    monkeypatch.setattr(settings, "PITR_DIFF_BACKUP_HOURS", 24)

    def keys(**changes) -> list[str]:
        return list(_alerts(Snapshot(archive=_archive(), scheduler=_scheduler(**changes))))

    assert keys(state="disabled", updated_at=NOW - timedelta(days=9)) == ["scheduler_disabled"]
    assert keys(updated_at=NOW - timedelta(minutes=10)) == ["scheduler_stalled"]  # 3 vueltas de 60 s
    running = {"state": "running", "updated_at": NOW - timedelta(minutes=50)}
    assert keys(**running, running_since=NOW - timedelta(minutes=50)) == []  # un respaldo largo, dentro de su tiempo
    assert keys(**running, running_since=NOW - timedelta(minutes=64)) == ["scheduler_stalled"]
    assert keys(state="running", running_since=None, updated_at=NOW - timedelta(minutes=5)) == ["scheduler_stalled"]
    assert keys(last_error="pgbackrest backup terminó con 82", last_error_at=NOW) == ["base_backup_failed"]
    assert keys(last_backup_at=NOW - timedelta(hours=31)) == ["base_backup_stale"]  # 24 h + 1 h de tiempo límite
    assert keys(last_backup_at=None, last_full_at=None) == []  # aún no hay respaldos: lo dice su error si falla
    monkeypatch.setattr(settings, "PITR_DIFF_BACKUP_HOURS", 0)  # solo completos: se espera uno por semana
    assert keys(last_backup_at=NOW - timedelta(hours=31)) == []


def test_missing_or_unreadable_state_and_unreachable_postgres():
    assert _alerts(Snapshot()) == {}  # nada que revisar todavía (recién arrancado)
    early = Snapshot(archive=_archive())
    assert _alerts(early, watching_since=NOW - timedelta(seconds=30)) == {}  # el servicio pitr apenas arranca
    assert list(_alerts(early, watching_since=NOW - timedelta(hours=1))) == ["scheduler_missing"]
    unreadable = Snapshot(archive_error="OperationalError: sin base", scheduler_error="scheduler.json: JSONDecodeError")
    found = _alerts(unreadable)
    assert set(found) == {"archive_unreadable", "scheduler_unreadable"} and "sin base" in found["archive_unreadable"]


class _Connection:
    def __init__(self, row: dict):
        self.row, self.sql = row, []

    @contextmanager
    def begin(self):
        yield

    def execute(self, statement, params=None):
        self.sql.append((str(statement), params))
        return self

    def mappings(self):
        return self

    def one(self):
        return self.row


class _Engine:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connect(self):
        if isinstance(self.conn, Exception):
            raise self.conn
        yield self.conn


def _row() -> dict:
    state = _archive(ready_segments=3, oldest_ready_at=NOW - timedelta(minutes=1))
    return {name: getattr(state, name) for name in ArchiveState.__dataclass_fields__}


def test_the_query_runs_in_its_own_transaction_with_a_time_limit():
    conn = _Connection(_row())
    assert archive_repository.archive_stats(conn, 15000)["ready_segments"] == 3
    (timeout, params), (stats, _) = conn.sql
    assert "set_config('statement_timeout'" in timeout and params == {"ms": "15000"}
    assert "pg_stat_archiver" in stats and "pg_ls_archive_statusdir()" in stats
    # Cada campo del estado sale con su nombre (pg_stat_archiver dice last_archived_time: va con su alias).
    assert [name for name in ArchiveState.__dataclass_fields__ if not re.search(rf"\b{name}\b", stats)] == []


@pytest.mark.skipif(not OWNER_URL, reason="Con PostgreSQL real (quality.sh --postgres); el simulacro la corre siempre")
def test_the_query_runs_on_postgresql():
    with owner_engine.connect() as conn:  # el dueño: pg_ls_archive_statusdir es de superusuario o pg_monitor
        state = ArchiveState.from_row(archive_repository.archive_stats(conn, 5000))
    assert state.archive_mode in {"on", "off", "always"} and state.segment_bytes > 0 and state.ready_segments >= 0


def test_a_snapshot_reads_postgres_and_the_shared_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PITR_STATUS_DIR", str(tmp_path))
    epoch = int(NOW.timestamp())
    status = {"updated_at": epoch, "state": "ok", "last_full_at": epoch - 60, "last_backup_at": epoch - 60}
    (tmp_path / pitr_monitor.SCHEDULER_FILE).write_text(json.dumps(status | {"last_error": None, "backups": 1}))
    (tmp_path / pitr_monitor.DROPPED_FILE).write_text(json.dumps({"first": epoch, "last": epoch, "count": 2}))
    built = []
    monkeypatch.setattr(pitr_monitor, "build_engine", lambda url: built.append(url) or _Engine(_Connection(_row())))
    snapshot = PitrMonitor().snapshot()
    assert built == [settings.DATABASE_DIRECT_URL]  # el dueño, directo: pg_ls_archive_statusdir es de superusuario
    assert snapshot.archive is not None and snapshot.archive.ready_segments == 3 and snapshot.archive_error is None
    assert snapshot.scheduler == SchedulerState(
        NOW, "ok", last_full_at=NOW - timedelta(minutes=1), backups=1, last_backup_at=NOW - timedelta(minutes=1)
    )
    assert snapshot.dropped == Dropped(NOW, NOW, 2, "")
    (tmp_path / pitr_monitor.SCHEDULER_FILE).write_text("[1, 2]")
    (tmp_path / pitr_monitor.DROPPED_FILE).write_text("{roto")
    broken = PitrMonitor(engine=_Engine(OSError("sin red"))).snapshot()
    assert broken.archive is None and broken.archive_error == "OSError: sin red"
    assert broken.scheduler_error == "scheduler.json: ValueError: no es un objeto JSON"
    assert broken.dropped_error is not None and broken.dropped_error.startswith("wal-dropped.json: JSONDecodeError")
    (tmp_path / pitr_monitor.SCHEDULER_FILE).unlink()
    (tmp_path / pitr_monitor.DROPPED_FILE).unlink()
    empty = PitrMonitor(engine=_Engine(_Connection(_row()))).snapshot()
    assert (empty.scheduler, empty.scheduler_error, empty.dropped, empty.dropped_error) == (None, None, None, None)


def test_each_alert_reaches_the_admin_once_per_window_and_its_recovery_is_noted(monkeypatch, caplog):
    reporter = ErrorReporter(capacity=100)
    handler = ErrorLogHandler(reporter)
    logging.getLogger("app.pitr").addHandler(handler)
    try:
        monkeypatch.setattr(settings, "PITR_ALERT_REPEAT_MINUTES", 60)
        snapshots = iter(
            [
                Snapshot(archive=_archive(archive_mode="off"), scheduler=_scheduler()),
                Snapshot(archive=_archive(archive_mode="off"), scheduler=_scheduler()),
                Snapshot(archive=_archive(archive_mode="off"), scheduler=_scheduler()),
                Snapshot(archive=_archive(), scheduler=_scheduler()),
            ]
        )
        moments = iter([0.0, 60.0, 3700.0, 3760.0])
        monitor = PitrMonitor(engine=None, clock=lambda: NOW, monotonic=lambda: next(moments), watching_since=NOW)
        monkeypatch.setattr(monitor, "snapshot", lambda: next(snapshots))
        caplog.set_level(logging.INFO)
        assert [a.key for a in monitor.check()] == ["archive_off"]  # se registra
        assert [a.key for a in monitor.check()] == ["archive_off"]  # un minuto después: no se repite
        assert [a.key for a in monitor.check()] == ["archive_off"]  # pasó la ventana: se repite
        assert monitor.check() == [] and monitor.active == {}  # se normalizó
        events = reporter._events  # lo que iría a "Errores del sistema"
        assert [(e.code, e.severity, e.source) for e in events] == [("app.pitr.archive_off", "CRITICAL", "LOG")] * 2
        assert "se normalizó el aviso archive_off" in caplog.text
    finally:
        logging.getLogger("app.pitr").removeHandler(handler)


def test_the_monitor_never_raises(monkeypatch, caplog):
    def broken(*_args, **_kwargs):
        raise RuntimeError("regla rota")

    monitor = PitrMonitor(engine=_Engine(_Connection(_row())))
    monkeypatch.setattr(pitr_monitor, "evaluate", broken)
    assert monitor.check() == [] and "Falló la revisión de la recuperación" in caplog.text


class _FrozenClock(datetime):
    """`datetime` con "ahora" fijo en `NOW`: el estado de ejemplo se arma contra NOW, y con el reloj real la prueba
    dejaba de pasar unas horas después de escrita (el servicio pitr "dejaba de reportar")."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return NOW


def test_pitr_status_shows_the_state_and_the_alerts(monkeypatch, capsys):
    monkeypatch.setattr(cli, "datetime", _FrozenClock)
    monkeypatch.setattr(settings, "PITR_ENABLED", False)
    assert cli._db_pitr_status(argparse.Namespace()) == 0 and "PITR apagado" in capsys.readouterr().out
    monkeypatch.setattr(settings, "PITR_ENABLED", True)
    healthy = Snapshot(archive=_archive(), scheduler=_scheduler())
    monkeypatch.setattr(PitrMonitor, "snapshot", lambda self: healthy)
    assert cli._db_pitr_status(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "archive_mode: on" in out and "servicio pitr: ok" in out and "300.00 MB en el bucket" in out
    assert "Sin avisos" in out
    dropped = Dropped(NOW, NOW, 3, "x")
    broken = Snapshot(archive_error="OperationalError: sin base", dropped=dropped)
    monkeypatch.setattr(PitrMonitor, "snapshot", lambda self: broken)
    assert cli._db_pitr_status(argparse.Namespace()) == 1
    out = capsys.readouterr().out
    assert "PostgreSQL: OperationalError: sin base" in out and "servicio pitr: sin estado todavía" in out
    assert "WAL descartado por el guardián: 3 segmentos" in out and "AVISO wal_dropped" in out and "aviso(s)" in out
    assert describe(Snapshot(scheduler_error="scheduler.json: roto"))[1] == "servicio pitr: scheduler.json: roto"
    assert Alert("k", "m") == Alert("k", "m")


def test_the_repository_key_is_long_and_on_one_line():
    from pydantic import ValidationError

    from app.core.config import Settings

    base = {"_env_file": None, "DATABASE_URL": "sqlite://", "POSTGRES_PASSWORD": "x"}
    assert Settings(**base, PITR_CIPHER_PASS="").PITR_CIPHER_PASS == ""  # sin PITR no hace falta
    assert Settings(**base, PITR_CIPHER_PASS="k" * 32).PITR_CIPHER_PASS == "k" * 32
    for bad in ("corta", "con espacios " * 4):
        with pytest.raises(ValidationError, match="PITR_CIPHER_PASS"):
            Settings(**base, PITR_CIPHER_PASS=bad)
    with pytest.raises(ValidationError, match="PITR_WAL_ALERT_MB no puede ser mayor que PITR_WAL_MAX_MB"):
        Settings(**base, PITR_WAL_ALERT_MB=9000, PITR_WAL_MAX_MB=8192)
