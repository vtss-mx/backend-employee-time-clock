"""Apagado ordenado de un proceso de la API: escalar hacia abajo, desplegar o reiniciar una réplica sin
cortar a nadie.

Sin esto, al recibir SIGTERM uvicorn deja de aceptar conexiones de inmediato: un balanceador que revisa la
readiness cada pocos segundos todavía le manda peticiones (fallan) y el proxy puede reutilizar una
conexión persistente justo cuando se cierra (un POST ya enviado no se reintenta). Con el drenado:

1. SIGTERM marca el proceso como "drenando": la readiness responde 503 `SHUTTING_DOWN` (el balanceador lo
   saca) y cada respuesta lleva `Connection: close` (el proxy deja de reutilizar sus conexiones), pero se
   sigue atendiendo normalmente cada petición durante `SHUTDOWN_DRAIN_SECONDS`.
2. Después la señal llega a uvicorn: deja de aceptar conexiones (el proxy reintenta en otra réplica, la
   petición nunca se envió), termina lo que lleva (`--timeout-graceful-shutdown`) y el `lifespan` detiene el
   mantenimiento y guarda el consumo y los errores pendientes.

Funciona igual con uno o varios procesos por réplica: en modo `--workers N` el proceso padre reenvía SIGTERM
a cada worker y cada uno drena por su cuenta. El estado es de este proceso (un `threading.Event`): no se
comparte ni hace falta.
"""

import logging
import signal
import threading
from collections.abc import Callable
from types import FrameType
from typing import Any

logger = logging.getLogger(__name__)

_draining = threading.Event()


def draining() -> bool:
    """¿Este proceso está por apagarse? (readiness 503 y `Connection: close`)."""
    return _draining.is_set()


def reset() -> None:
    """Vuelve al estado normal (pruebas: cada una empieza sin drenar)."""
    _draining.clear()


def start_draining() -> None:
    """Marca el proceso como drenando (lo llama el manejador de SIGTERM)."""
    if not _draining.is_set():
        logger.info("Apagado ordenado: este proceso deja de recibir tráfico nuevo y termina lo que lleva")
    _draining.set()


def install_drain(delay: float) -> bool:
    """Envuelve el manejador de SIGTERM que instaló el servidor (uvicorn) para drenar `delay` segundos antes
    de entregarle la señal. Devuelve si quedó instalado.

    No se instala (y el apagado es el de siempre) con `delay` = 0, fuera del hilo principal (las señales
    solo se atienden ahí; p. ej. el TestClient) o si no hay un manejador del servidor que envolver (la línea
    de comandos, un proceso sin uvicorn). Una segunda señal apaga sin esperar más."""
    if delay <= 0 or threading.current_thread() is not threading.main_thread():
        return False
    previous = signal.getsignal(signal.SIGTERM)
    if not callable(previous):
        return False
    server_exit: Callable[[int, FrameType | None], Any] = previous

    def on_sigterm(sig: int, frame: FrameType | None) -> None:
        if draining():
            server_exit(sig, frame)
            return
        start_draining()
        timer = threading.Timer(delay, server_exit, args=(sig, frame))
        timer.daemon = True
        timer.start()

    signal.signal(signal.SIGTERM, on_sigterm)
    return True
