"""Arranque y apagado de la API (app/main.py): tolerante a fallas y en orden.

La API debe iniciar aunque la BD aún no responda, el alta de usuarios iniciales falle o los modelos
faciales no carguen (readiness lo refleja y se recupera sola); al apagar, el mantenimiento se detiene
antes que el registro de errores para que lo último que reporte también se guarde.
"""

import asyncio
from types import SimpleNamespace

import anyio.to_thread
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.facial_recognition
from app import main
from app.core.config import settings


@pytest.fixture
def startup(monkeypatch):
    """Dependencias del arranque simuladas; `events` guarda en orden lo que ocurrió."""
    events: list[str] = []

    def component(name: str):
        class _Background:
            """Hilo en segundo plano simulado (registro de errores en lotes, mantenimiento)."""

            def __init__(self, interval: float) -> None:
                events.append(f"{name}({interval:g})")

            def start(self) -> None:
                events.append(f"{name}.start")

            def stop(self) -> None:
                events.append(f"{name}.stop")

        return _Background

    monkeypatch.setattr(main, "install_log_handler", lambda: events.append("log_handler"))
    monkeypatch.setattr(main, "ErrorReportFlusher", component("flusher"))
    monkeypatch.setattr(main, "UsageFlusher", component("meter"))
    monkeypatch.setattr(main, "PerfFlusher", component("perf"))
    monkeypatch.setattr(main, "MaintenanceScheduler", component("scheduler"))
    monkeypatch.setattr(main, "wait_for_database", lambda retries: events.append("db") or True)
    monkeypatch.setattr(main, "ensure_first_admin", lambda _db: events.append("admin"))
    monkeypatch.setattr(main, "ensure_first_company", lambda _db: events.append("company"))
    monkeypatch.setattr(main, "statement_timeout_missing", lambda: events.append("timeout") or False)
    monkeypatch.setattr(main, "install_drain", lambda delay: events.append(f"drain({delay:g})") or True)
    cache = SimpleNamespace(close=lambda: events.append("cache.close"))
    monkeypatch.setattr(main, "shared_cache", lambda: events.append("cache") or cache)
    monkeypatch.setattr(settings, "SHUTDOWN_DRAIN_SECONDS", 5.0)
    monkeypatch.setattr(settings, "THREADPOOL_SIZE", 0)
    monkeypatch.setattr(settings, "MAX_CONCURRENT_REQUESTS", 100)
    return events


def _face_pool(monkeypatch, *, workers: int = 0, max_waiting: int = 0, error: Exception | None = None) -> None:
    def face_pool():
        if error is not None:
            raise error
        return SimpleNamespace(stats=lambda: SimpleNamespace(workers=workers, max_waiting=max_waiting))

    monkeypatch.setattr(app.facial_recognition, "face_pool", face_pool)


def _serve(events: list[str]) -> int:
    """Arranca, "atiende" (anota los hilos disponibles) y apaga; devuelve el tamaño del threadpool."""

    async def run() -> int:
        async with main.lifespan(FastAPI()):
            events.append("serving")
            return anyio.to_thread.current_default_thread_limiter().total_tokens

    return asyncio.run(run())


def test_full_start_runs_everything_in_order_and_stops_in_reverse(startup, monkeypatch):
    monkeypatch.setattr(settings, "ERROR_REPORT_FLUSH_SECONDS", 2.0)
    monkeypatch.setattr(settings, "USAGE_FLUSH_SECONDS", 5.0)
    monkeypatch.setattr(settings, "PERF_FLUSH_SECONDS", 15.0)
    monkeypatch.setattr(settings, "MAINTENANCE_INTERVAL_SECONDS", 300)
    _face_pool(monkeypatch, workers=8, max_waiting=32)
    threads = _serve(startup)
    assert startup == [
        "log_handler",  # primero: hasta los errores del arranque se registran
        "flusher(2)",
        "flusher.start",
        "meter(5)",  # el medidor de consumo guarda en lotes desde el arranque
        "meter.start",
        "perf(15)",  # y el rendimiento (rutas, funciones, navegador y peticiones lentas)
        "perf.start",
        "db",
        "timeout",  # ¿las consultas tienen tiempo límite? (con PgBouncer lo pone su configuración)
        "admin",
        "company",
        "cache",  # la caché compartida (Redis) dice una vez dónde está o que está apagada
        "scheduler(300)",
        "scheduler.start",
        "drain(5)",  # al final del arranque: el manejador de SIGTERM del servidor ya existe
        "serving",
        "scheduler.stop",
        "meter.stop",  # guarda el consumo pendiente
        "perf.stop",  # y el rendimiento pendiente
        "flusher.stop",  # al final: guarda lo que el mantenimiento reportó al detenerse
        "cache.close",  # y el pool de Redis, ya sin nadie que lo use
    ]
    # Más hilos que peticiones admitidas + espacio para la fila facial: nadie bloquea el login.
    assert threads == 100 + max(16, (8 + 32) // 4)


def test_start_without_database_nor_face_models_still_serves(startup, monkeypatch, caplog):
    """BD caída y sin modelos: la API inicia igual (sin hilos en segundo plano si están apagados)."""
    monkeypatch.setattr(settings, "ERROR_REPORT_FLUSH_SECONDS", 0)
    monkeypatch.setattr(settings, "MAINTENANCE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(main, "wait_for_database", lambda retries: startup.append(f"db x{retries}") and False)
    _face_pool(monkeypatch, error=RuntimeError("modelo no encontrado"))
    threads = _serve(startup)
    # Sin alta de usuarios ni revisión del tiempo límite (no hay BD); el drenado sí (no depende de ella).
    retries = settings.DB_STARTUP_RETRIES
    assert startup == ["log_handler", f"db x{retries}", "cache", "drain(5)", "serving", "cache.close"]
    assert threads >= 100 + 16
    assert "La base de datos no respondió al iniciar" in caplog.text
    assert (
        "No se pudieron cargar los modelos faciales (se reintentará bajo demanda): modelo no encontrado" in caplog.text
    )


def test_failing_initial_users_do_not_stop_the_start(startup, monkeypatch, caplog):
    def broken(_db):
        raise RuntimeError("tabla bloqueada")

    monkeypatch.setattr(main, "ensure_first_admin", broken)
    _face_pool(monkeypatch, workers=1, max_waiting=4)
    _serve(startup)
    assert "serving" in startup and "company" not in startup
    assert "No se pudieron verificar los usuarios iniciales" in caplog.text


def test_queries_without_a_time_limit_are_reported_at_startup(startup, monkeypatch, caplog):
    """Un PgBouncer sin connect_query dejaría cada consulta sin límite: es un error para el ADMIN, no un aviso."""
    monkeypatch.setattr(main, "statement_timeout_missing", lambda: True)
    _face_pool(monkeypatch, workers=1, max_waiting=4)
    _serve(startup)
    assert "serving" in startup
    assert any(r.levelname == "ERROR" and "no tienen statement_timeout" in r.message for r in caplog.records)


def test_a_failing_time_limit_check_does_not_stop_the_start(startup, monkeypatch, caplog):
    def broken():
        raise RuntimeError("BD parpadeó")

    monkeypatch.setattr(main, "statement_timeout_missing", broken)
    _face_pool(monkeypatch, workers=1, max_waiting=4)
    _serve(startup)
    assert "serving" in startup and "company" in startup  # el arranque sigue
    assert "No se pudo revisar el tiempo límite de las consultas" in caplog.text


# ---------------------------------------------------------------- atajos a la documentación


def test_root_and_api_docs_lead_to_swagger_when_published(client):
    for path in ("/", f"{settings.API_PREFIX}/docs"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 307 and response.headers["location"] == "/docs"


def test_without_published_docs_the_shortcuts_do_not_exist():
    """DOCS_ENABLED=false (producción): ni la raíz ni /api/docs delatan que hubo documentación."""
    hidden = FastAPI()
    main.register_docs_redirects(hidden, enabled=False)
    with TestClient(hidden) as test_client:
        assert test_client.get("/", follow_redirects=False).status_code == 404
        assert test_client.get(f"{settings.API_PREFIX}/docs", follow_redirects=False).status_code == 404
