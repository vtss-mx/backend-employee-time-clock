"""Motor de base de datos y arranque tolerante (app/core/database.py)."""

import pytest
from sqlalchemy import event, text

from app.core import database
from app.core.config import settings
from app.core.db_schemas import ALL_SCHEMAS


def test_postgres_engine_bounds_every_wait_and_hides_personal_data():
    """Producción: pool acotado, conexiones verificadas, tiempo límite por consulta y por conexión,
    sin sentencias preparadas (PgBouncer) y sin parámetros en los errores. Crear el motor no conecta."""
    engine = database.build_engine("postgresql+psycopg://tc:secreto@db.invalid:5432/timeclock")
    connect: dict = {}

    @event.listens_for(engine, "do_connect")
    def _capture(_dialect, _record, _args, params):
        """Lo que recibiría psycopg al conectar (sin salir a la red)."""
        connect.update(params)
        raise ConnectionAbortedError

    try:
        assert engine.dialect.name == "postgresql" and engine.hide_parameters
        assert engine.pool.size() == settings.DB_POOL_SIZE and engine.pool._pre_ping
        assert engine.pool._timeout == settings.DB_POOL_TIMEOUT_SECONDS
        with pytest.raises(ConnectionAbortedError):
            engine.connect()
        assert connect["connect_timeout"] == settings.DB_CONNECT_TIMEOUT_SECONDS
        assert f"statement_timeout={settings.DB_STATEMENT_TIMEOUT_MS}" in connect["options"]
        assert all(schema in connect["options"] for schema in ALL_SCHEMAS)
        assert connect["prepare_threshold"] is None
    finally:
        engine.dispose()


def test_behind_pgbouncer_the_engine_sends_no_session_parameters():
    """PgBouncer en modo transacción rechaza parámetros de sesión al conectar ("unsupported startup parameter")
    y una sentencia preparada no sobrevive al cambio de conexión: ninguna de las dos cosas. El tiempo límite lo
    pone PgBouncer (connect_query) y se verifica al arrancar (`statement_timeout_missing`)."""
    engine = database.build_engine("postgresql+psycopg://tc:secreto@pgbouncer.invalid:6432/timeclock", pooled=True)
    connect: dict = {}

    @event.listens_for(engine, "do_connect")
    def _capture(_dialect, _record, _args, params):
        connect.update(params)
        raise ConnectionAbortedError

    try:
        with pytest.raises(ConnectionAbortedError):
            engine.connect()
        assert "options" not in connect
        assert connect["prepare_threshold"] is None
        assert connect["connect_timeout"] == settings.DB_CONNECT_TIMEOUT_SECONDS
        assert engine.pool.size() == settings.DB_POOL_SIZE  # el pool del proceso sigue acotado
    finally:
        engine.dispose()
    assert database.session_options(pooled=True) == {}


class _ShowEngine:
    """Motor de PostgreSQL simulado que responde `SHOW statement_timeout` con `value`."""

    dialect = type("Dialect", (), {"name": "postgresql"})()

    def __init__(self, value: str) -> None:
        self.value = value

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def execute(self, statement):
        assert str(statement) == "SHOW statement_timeout"
        return type("Result", (), {"scalar_one": lambda _self: self.value})()


@pytest.mark.parametrize(("value", "missing"), [("0", True), ("15s", False), ("15000ms", False)])
def test_startup_detects_queries_without_a_time_limit(monkeypatch, value, missing):
    """Un PgBouncer sin connect_query dejaría cada consulta sin límite en silencio: se detecta al arrancar."""
    monkeypatch.setattr(database, "engine", _ShowEngine(value))
    assert database.statement_timeout_missing() is missing


def test_the_test_database_never_reports_a_missing_time_limit():
    """SQLite no tiene statement_timeout que revisar; en PostgreSQL (TEST_DATABASE_URL) lo manda el motor."""
    assert database.statement_timeout_missing() is False


def test_sqlite_engine_enforces_foreign_keys_on_every_connection():
    """Las pruebas en SQLite respetan ON DELETE CASCADE y las restricciones como PostgreSQL."""
    engine = database.build_engine("sqlite://")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    finally:
        engine.dispose()


class _FlakyEngine:
    """Motor que falla las primeras `failures` conexiones (BD reiniciándose)."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0
        self.real = database.build_engine("sqlite://")

    def connect(self):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise ConnectionRefusedError("BD aún no lista")
        return self.real.connect()


@pytest.fixture
def flaky_database(monkeypatch):
    """Instala como motor del proceso uno que falla las primeras `failures` conexiones; las pausas
    entre intentos se anotan en `sleeps` en lugar de esperar de verdad."""
    sleeps: list[float] = []
    installed: list[_FlakyEngine] = []
    monkeypatch.setattr("time.sleep", sleeps.append)

    def install(failures: int) -> _FlakyEngine:
        flaky = _FlakyEngine(failures)
        installed.append(flaky)
        monkeypatch.setattr(database, "engine", flaky)
        return flaky

    yield install, sleeps
    for flaky in installed:
        flaky.real.dispose()


def test_startup_waits_for_the_database_with_growing_bounded_pauses(flaky_database, caplog):
    install, sleeps = flaky_database
    flaky = install(failures=2)
    assert database.wait_for_database(retries=5, delay=6) is True
    assert flaky.attempts == 3 and sleeps == [6, 10]  # crece con cada intento, nunca más de 10 s
    assert "intento 1/5" in caplog.text and "ConnectionRefusedError" in caplog.text


def test_startup_gives_up_after_the_retries_without_crashing(flaky_database):
    install, sleeps = flaky_database
    install(failures=10)
    assert database.wait_for_database(retries=3, delay=1) is False
    assert sleeps == [1, 2, 3]
