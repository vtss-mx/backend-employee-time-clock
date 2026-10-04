"""Middleware ASGI del control de admisión adaptativo (app/core/admission.py).

Cada petición HTTP (salvo las sondas de salud) pide lugar al controlador de su proceso según su
grupo de API: pasa, espera en la fila con su prioridad o recibe 503 SERVER_BUSY con Retry-After
(reintentable, con el contrato de respuesta único). Al terminar con éxito se registra cuánto tardó:
con eso el límite se ajusta solo a la capacidad real. Sin overhead de BaseHTTPMiddleware.
"""

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.admission import AdmissionController, group_of
from app.core.exceptions import error_response

logger = logging.getLogger(__name__)

_EXEMPT_SUFFIXES = ("/health/live", "/health/ready", "/health")


class AdaptiveAdmissionMiddleware:
    def __init__(self, app: ASGIApp, controller: AdmissionController, api_prefix: str = "/api") -> None:
        self.app = app
        self.controller = controller
        self.api_prefix = api_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].endswith(_EXEMPT_SUFFIXES):
            await self.app(scope, receive, send)
            return
        group = group_of(scope["method"], scope["path"], self.api_prefix)
        if not await self.controller.acquire(group):
            if self.controller.shed % 100 == 1:
                logger.warning("API saturada: %s peticiones descartadas (%s)", self.controller.shed, group)
            response = error_response(
                503,
                "SERVER_BUSY",
                "El servicio está atendiendo muchas solicitudes. Intenta nuevamente en unos segundos.",
                headers={"Retry-After": str(self.controller.retry_after())},
            )
            await response(scope, receive, send)
            return
        started = time.perf_counter()
        status = 0

        async def send_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        finally:
            # Solo una respuesta exitosa mide la capacidad: un 401 o un 503 inmediatos (o una falla que
            # tardó lo que su tiempo límite) desviarían la latencia típica.
            elapsed = time.perf_counter() - started if 200 <= status < 400 else None
            self.controller.release(group, elapsed)
