"""Asigna un identificador a cada petición (X-Request-ID = traceId) para correlacionar logs y respuestas.

Middleware ASGI puro: no usa BaseHTTPMiddleware (que crea tareas y streams adicionales por
petición y reduce el rendimiento con alta concurrencia).
"""

import logging
import re
import time

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import internal_error_response
from app.core.request_context import request_id_var
from app.core.responses import new_trace_id

logger = logging.getLogger("app.access")
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = ""
        for key, value in scope.get("headers", []):
            if key == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        rid = incoming if _VALID_ID.match(incoming) else new_trace_id()
        token = request_id_var.set(rid)
        start = time.perf_counter()
        started = False

        async def send_with_id(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            # Error no controlado: se responde aquí (no en ServerErrorMiddleware) para que el
            # cuerpo y la cabecera conserven el traceId de la petición.
            logger.exception("Error no controlado en %s %s", scope.get("method"), scope.get("path"))
            if started:
                raise
            await internal_error_response()(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
            elapsed = (time.perf_counter() - start) * 1000
            if elapsed > 3000:
                logger.warning(
                    "Petición lenta %s %s %.0f ms [%s]", scope.get("method"), scope.get("path"), elapsed, rid
                )


def register_request_id_middleware(app: FastAPI) -> None:
    app.add_middleware(RequestIdMiddleware)
