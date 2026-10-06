"""Apagado ordenado (app/core/lifecycle.py): escalar hacia abajo o desplegar sin cortar peticiones.

Al recibir SIGTERM el proceso sigue atendiendo, pero su readiness dice 503 `SHUTTING_DOWN` (el balanceador lo
saca) y cada respuesta cierra su conexión (el proxy no reutiliza una que está por cerrarse); después de
`SHUTDOWN_DRAIN_SECONDS` la señal llega al servidor, que termina lo que lleva.
"""

import signal
import threading

import pytest

from app.core import lifecycle


@pytest.fixture(autouse=True)
def _normal_state():
    """Cada prueba empieza y termina sin drenar y con el manejador de SIGTERM que tenía el proceso."""
    original = signal.getsignal(signal.SIGTERM)
    lifecycle.reset()
    yield
    lifecycle.reset()
    signal.signal(signal.SIGTERM, original)


class _Server:
    """El manejador de SIGTERM del servidor (uvicorn): anota cuándo se le entregó la señal."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.delivered = threading.Event()

    def handle_exit(self, sig: int, _frame: object) -> None:
        self.calls.append(sig)
        self.delivered.set()


def test_sigterm_first_drains_and_then_reaches_the_server():
    server = _Server()
    signal.signal(signal.SIGTERM, server.handle_exit)
    assert lifecycle.install_drain(0.05) is True
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler) and not lifecycle.draining()

    handler(signal.SIGTERM, None)  # como lo haría el sistema operativo
    assert lifecycle.draining()  # de inmediato: readiness 503 y Connection: close
    assert server.calls == []  # pero el servidor sigue atendiendo durante el drenado
    assert server.delivered.wait(2)  # ...y al terminarlo recibe la señal y se apaga ordenadamente
    assert server.calls == [signal.SIGTERM]


def test_a_second_sigterm_stops_without_waiting():
    server = _Server()
    signal.signal(signal.SIGTERM, server.handle_exit)
    lifecycle.install_drain(60)
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)
    handler(signal.SIGTERM, None)  # el orquestador insiste (o alguien lo pide a mano): ya no se espera
    assert server.calls == [signal.SIGTERM]


def test_without_a_server_handler_nor_drain_time_nothing_changes():
    """Sin uvicorn (línea de comandos, pruebas) o con SHUTDOWN_DRAIN_SECONDS=0, el apagado es el de siempre."""
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    assert lifecycle.install_drain(5) is False
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    signal.signal(signal.SIGTERM, _Server().handle_exit)
    assert lifecycle.install_drain(0) is False


def test_signals_are_only_installed_from_the_main_thread():
    """Python solo atiende señales en el hilo principal (p. ej. el lifespan del TestClient corre en otro)."""
    signal.signal(signal.SIGTERM, _Server().handle_exit)
    result: list[bool] = []
    worker = threading.Thread(target=lambda: result.append(lifecycle.install_drain(5)))
    worker.start()
    worker.join()
    assert result == [False]


def test_while_draining_readiness_says_shutting_down_and_connections_close(client):
    assert client.get("/api/health/ready").status_code == 200
    lifecycle.start_draining()
    lifecycle.start_draining()  # repetirlo no cambia nada
    ready = client.get("/api/health/ready")
    body = ready.json()
    assert ready.status_code == 503 and ready.headers["Retry-After"] == "1"
    assert body["code"] == "SHUTTING_DOWN" and body["errors"][0]["code"] == "SHUTTING_DOWN"
    assert body["data"] == {"status": "shutting_down"} and body["traceId"]
    assert ready.headers["connection"] == "close"
    # Lo demás se sigue atendiendo normalmente, solo sin reutilizar la conexión.
    live = client.get("/api/health/live")
    assert live.status_code == 200 and live.headers["connection"] == "close"
