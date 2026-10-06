"""Mantenimiento: lo vencido se depura fuera de las peticiones, en lotes y sin tocar lo vigente."""

import logging
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import AuthSession, CaptureFingerprint, FaceChallenge, RateLimitCounter, RememberedAccount, User
from app.services import maintenance_service
from app.services.maintenance_service import MaintenanceScheduler, purge_expired, run_once


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_purges_only_what_expired_in_batches(client, company_headers):
    now = datetime.now(UTC)
    old, fresh = now - timedelta(days=settings.FACE_REPLAY_RETENTION_DAYS + 1), now
    with SessionLocal() as db:
        user_id = db.scalar(select(User.id).where(User.email == "admin@empresa.com"))
        db.add_all(CaptureFingerprint(digest=f"{i:064x}", company_id=1, created_at=old) for i in range(7))
        db.add(CaptureFingerprint(digest="f" * 64, company_id=1, created_at=fresh))
        db.add(FaceChallenge(id="viejo", user_id=user_id, direction="TURN_LEFT", expires_at=now - timedelta(minutes=1)))
        db.add(FaceChallenge(id="nuevo", user_id=user_id, direction="TURN_LEFT", expires_at=now + timedelta(minutes=1)))
        db.add(RateLimitCounter(key="login:x:1", count=3, expires_at=now - timedelta(hours=1)))
        db.add(RememberedAccount(id="r1", token_hash="h" * 64, user_id=user_id, created_at=old, expires_at=old))
        db.commit()
        sessions_before = _count(db, AuthSession)

        removed = purge_expired(db, batch_size=3)  # 7 huellas viejas: tres lotes (3 + 3 + 1)
        assert removed["huellas de capturas"] == 7
        assert removed["retos de prueba de vida"] == 1
        assert removed["límites de peticiones"] == 1 and removed["cuentas recordadas"] == 1
        assert _count(db, CaptureFingerprint) == 1 and db.get(FaceChallenge, "nuevo") is not None
        assert _count(db, AuthSession) == sessions_before  # la sesión vigente no se toca
        assert all(count == 0 for count in purge_expired(db).values())  # nada pendiente


def test_one_round_at_a_time_and_scheduler(monkeypatch):
    assert run_once() is not None  # SQLite (pruebas): sin candado, siempre trabaja
    calls: list[int] = []
    monkeypatch.setattr(maintenance_service, "run_once", lambda: calls.append(1) or {"sesiones": 1})
    scheduler = MaintenanceScheduler(0.01)
    scheduler.start()
    for _ in range(100):
        if calls:
            break
        scheduler._stop.wait(0.01)
    scheduler.stop()
    assert calls


def _purge_name(key) -> str:
    """Nombre de la depuración de una tabla (sin depender de cuántas hay ni de su orden)."""
    return next(p.name for p in maintenance_service.PURGES if p.key is key)


def _expired_counters(count: int) -> None:
    expired = datetime.now(UTC) - timedelta(hours=1)
    with SessionLocal() as db:
        db.add_all(RateLimitCounter(key=f"login:x:{i}", count=1, expires_at=expired) for i in range(count))
        db.commit()


def test_a_round_stops_at_the_batch_cap_and_the_next_one_continues(monkeypatch):
    """Una tabla con muchísimo vencido no acapara la vuelta: a lo más MAINTENANCE_MAX_BATCHES_PER_TABLE lotes
    y lo demás sale en la siguiente."""
    monkeypatch.setattr(settings, "MAINTENANCE_MAX_BATCHES_PER_TABLE", 2)
    name = _purge_name(RateLimitCounter.key)
    _expired_counters(5)
    with SessionLocal() as db:
        assert purge_expired(db, batch_size=2)[name] == 4  # dos lotes completos: el tope de la vuelta
        assert _count(db, RateLimitCounter) == 1
        assert purge_expired(db, batch_size=2)[name] == 1


class _AdvisoryLock:
    """Motor y conexión de PostgreSQL simulados SOLO para el candado de asesoría (la depuración sigue
    usando la BD de pruebas). `granted` es lo que respondería `pg_try_advisory_xact_lock`: False = otra
    instancia está depurando. Salir del `with` de la conexión termina su transacción (y suelta el candado):
    se anota como "fin"."""

    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, granted: bool) -> None:
        self.granted = granted
        self.calls: list[str] = []

    def connect(self) -> _AdvisoryLock:
        return self

    def __enter__(self) -> _AdvisoryLock:
        return self

    def __exit__(self, *_exc) -> None:
        self.calls.append("fin")

    def execute(self, statement, params=None):
        if params is None:  # el candado apaga para sí el límite de transacción inactiva del rol de la API
            assert str(statement) == "SET LOCAL idle_in_transaction_session_timeout = 0"
            self.calls.append("SET LOCAL")
            return None
        assert params == {"key": maintenance_service.MAINTENANCE_LOCK_KEY}
        self.calls.append(str(statement).removeprefix("SELECT ").split("(")[0])
        return SimpleNamespace(scalar=lambda: self.granted)


def test_on_postgresql_only_the_instance_holding_the_lock_purges(monkeypatch):
    name = _purge_name(RateLimitCounter.key)
    _expired_counters(1)

    busy = _AdvisoryLock(granted=False)
    monkeypatch.setattr(maintenance_service, "engine", busy)
    assert run_once() is None  # otra instancia tiene el candado: esta se salta la vuelta
    assert busy.calls == ["pg_try_advisory_xact_lock", "fin"]  # nada más: no toca un candado que no es suyo
    with SessionLocal() as db:
        assert _count(db, RateLimitCounter) == 1

    holder = _AdvisoryLock(granted=True)
    monkeypatch.setattr(maintenance_service, "engine", holder)
    removed = run_once()
    assert removed is not None and removed[name] == 1
    # De transacción: se suelta al terminar la transacción de su conexión (también tras PgBouncer en modo
    # transacción, donde un candado de sesión quedaría pegado a una conexión que luego usa otro cliente).
    assert holder.calls == ["pg_try_advisory_xact_lock", "SET LOCAL", "fin"]


def test_the_lock_is_released_even_if_the_round_fails(monkeypatch):
    holder = _AdvisoryLock(granted=True)
    monkeypatch.setattr(maintenance_service, "engine", holder)

    def broken(_db):
        raise RuntimeError("falla inesperada")

    monkeypatch.setattr(maintenance_service, "purge_expired", broken)
    with pytest.raises(RuntimeError):
        run_once()
    assert holder.calls == ["pg_try_advisory_xact_lock", "SET LOCAL", "fin"]  # las demás instancias siguen


def test_the_scheduler_survives_failures_and_only_logs_real_work(monkeypatch, caplog):
    """La BD caída en una vuelta no detiene el hilo; una vuelta saltada o sin nada que borrar no
    llena el log."""
    outcomes: list = [RuntimeError("BD caída"), None, {"tabla": 0}, {"tabla": 2}]
    finished = threading.Event()

    def next_round():
        if not outcomes:
            finished.set()
            return None
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(maintenance_service, "run_once", next_round)
    scheduler = MaintenanceScheduler(0.01)
    with caplog.at_level(logging.INFO, logger=maintenance_service.__name__):
        scheduler.start()
        try:
            assert finished.wait(5)
        finally:
            scheduler.stop()
    messages = [r.getMessage() for r in caplog.records if r.name == maintenance_service.__name__]
    assert messages.count("Falló el mantenimiento programado") == 1
    assert [m for m in messages if m.startswith("Mantenimiento:")] == ["Mantenimiento: tabla=2"]
    assert not scheduler._thread.is_alive()
