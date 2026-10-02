"""Control de admisión: limita las peticiones procesándose a la vez (backpressure).

Con miles de usuarios conectados, aceptar todo sin límite agota hilos, conexiones a la BD
y memoria, y TODAS las peticiones se vuelven lentas. Este middleware ASGI (sin overhead de
BaseHTTPMiddleware) admite hasta MAX_CONCURRENT_REQUESTS; las demás esperan en orden un
tiempo acotado y, si no hay capacidad, reciben 503 SERVER_BUSY con Retry-After y el contrato
de respuesta único. Las sondas de salud no se limitan.
"""

import asyncio
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.exceptions import error_response

logger = logging.getLogger(__name__)

_EXEMPT_SUFFIXES = ("/health/live", "/health/ready", "/health")


class ConcurrencyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_concurrent: int, queue_timeout: float) -> None:
        self.app = app
        self.max_concurrent = max_concurrent
        self.queue_timeout = queue_timeout
        self._semaphore: asyncio.Semaphore | None = None
        self.in_flight = 0
        self.rejected = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].endswith(_EXEMPT_SUFFIXES):
            await self.app(scope, receive, send)
            return
        if self._semaphore is None:  # se crea dentro del event loop activo
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.queue_timeout)
        except TimeoutError:
            self.rejected += 1
            if self.rejected % 100 == 1:
                logger.warning("API saturada: %s peticiones rechazadas por capacidad", self.rejected)
            response = error_response(
                503,
                "SERVER_BUSY",
                "El servicio está atendiendo muchas solicitudes. Intenta nuevamente en unos segundos.",
                headers={"Retry-After": "3"},
            )
            await response(scope, receive, send)
            return
        self.in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1
            self._semaphore.release()
