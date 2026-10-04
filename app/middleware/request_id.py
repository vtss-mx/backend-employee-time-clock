"""Asigna un identificador a cada petición (X-Request-ID = traceId) para correlacionar logs y respuestas,
y registra en ops.error_reports la falla del servidor con que terminó (si la hubo).

Solo las fallas del servidor (5xx y excepciones no controladas) van a la bandeja del ADMIN; un 4xx es
un resultado normal y queda en el log del proceso con su código y traceId (`app/core/error_events.py`).

Middleware ASGI puro: no usa BaseHTTPMiddleware (que crea tareas y streams adicionales por
petición y reduce el rendimiento con alta concurrencia).
"""

import logging
import re
import time
import traceback
from typing import Any

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.error_context import CAPTURE_LIMIT, BodyCapture, headers_of, parse_body, query_of
from app.core.error_events import ErrorEvent, is_recorded, route_of, severity_for
from app.core.exceptions import internal_error_response
from app.core.request_context import RequestInfo, request_id_var, request_info_var
from app.core.responses import new_trace_id
from app.services.error_reporter import error_reporter

logger = logging.getLogger("app.access")
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class _Exchange:
    """Lo que se pidió y lo que se respondió, copiado mientras pasa (para el contexto de un error):
    el cuerpo de la petición (acotado, sin archivos) y, si fue una falla que se registra (5xx), el de
    la respuesta. El cuerpo de un 4xx no se copia: no se registra."""

    def __init__(self, scope: Scope) -> None:
        headers = scope.get("headers", [])
        content_type = next((v.decode("latin-1") for k, v in headers if k == b"content-type"), "")
        self.request = BodyCapture(content_type)
        self.status = 0
        self.response_type = ""
        self.response = b""

    def received(self, message: Message) -> None:
        if message["type"] == "http.request":
            self.request.feed(message.get("body", b""))

    def sent(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            types = [v for k, v in message.get("headers", []) if k.lower() == b"content-type"]
            self.response_type = types[0].decode("latin-1") if types else ""
        elif (
            message["type"] == "http.response.body"
            and is_recorded(severity_for(self.status))
            and len(self.response) < CAPTURE_LIMIT
        ):
            self.response += message.get("body", b"")[: CAPTURE_LIMIT - len(self.response)]

    def context(self, scope: Scope, info: RequestInfo, elapsed_ms: float) -> dict[str, Any]:
        """El contexto literal del error (sin secretos ni archivos)."""
        client = scope.get("client")
        request = {
            "method": scope.get("method"),
            "path": scope.get("path"),
            "query": query_of(scope.get("query_string", b"")),
            "ip": client[0] if client else None,
            "headers": headers_of(scope.get("headers", [])),
            "body": self.request.value(),
            "body_bytes": self.request.total,
            "body_truncated": self.request.truncated,
        }
        user = {"id": info.user_id, "email": info.user_email, "role": info.user_role} if info.user_id else None
        response = {
            "status": self.status or 500,
            "body": parse_body(self.response_type, self.response) if self.response else None,
        }
        return {
            "request": request,
            "response": response,
            "user": user,
            "company_id": info.company_id,
            "duration_ms": round(elapsed_ms, 1),
        }


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
        info = RequestInfo(method=scope.get("method"), path=scope.get("path"))
        info_token = request_info_var.set(info)
        start = time.perf_counter()
        started = False
        crash: Exception | None = None
        exchange = _Exchange(scope)

        async def receive_copy() -> Message:
            message = await receive()
            exchange.received(message)
            return message

        async def send_with_id(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            exchange.sent(message)
            await send(message)

        try:
            await self.app(scope, receive_copy, send_with_id)
        except Exception as exc:
            # Error no controlado: se responde aquí (no en ServerErrorMiddleware) para que el
            # cuerpo y la cabecera conserven el traceId de la petición.
            crash = exc
            logger.exception("Error no controlado en %s %s", scope.get("method"), scope.get("path"))
            if started:
                raise
            await internal_error_response()(scope, receive, send_with_id)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            try:
                _report(scope, info, crash, rid, exchange, elapsed)
            except Exception:  # registrar el error jamás rompe la respuesta (este log no se reporta)
                logger.exception("No se pudo registrar el error de %s %s", scope.get("method"), scope.get("path"))
            request_info_var.reset(info_token)
            request_id_var.reset(token)
            if elapsed > 3000:
                logger.warning(
                    "Petición lenta %s %s %.0f ms [%s]", scope.get("method"), scope.get("path"), elapsed, rid
                )


def location_of(scope: Scope) -> str:
    """Dónde ocurrió, con la plantilla de la ruta (`/api/employees/{employee_id}/qr`): los parámetros
    no multiplican los reportes. Sin ruta (la falla ocurrió antes de enrutar, p. ej. la saturación) se
    usa la URL sin ids."""
    path = scope.get("path", "")
    template = getattr(scope.get("route"), "path_format", None)
    if template is None:
        return route_of(path)
    parts, pattern = path.split("/"), template.split("/")
    offset = max(0, len(parts) - len(pattern))  # la plantilla no lleva el prefijo con que se montó (/api)
    named = [t if t.startswith("{") else p for p, t in zip(parts[offset:], pattern, strict=False)]
    return "/".join(parts[:offset] + named)[:255]


def _report(
    scope: Scope, info: RequestInfo, crash: Exception | None, trace_id: str, exchange: _Exchange, elapsed_ms: float
) -> None:
    """Registra la falla del servidor con que terminó la petición (si la hubo) en ops.error_reports,
    con su contexto literal: quién, qué pidió y qué se le respondió.

    Un 4xx no se registra (ni se arma su contexto, lo más costoso): es un resultado normal que se
    respondió con su código. Queda una línea INFO en el log del proceso para no silenciar nada."""
    if crash is None and info.error is None:
        return
    status, code, message = info.error if crash is None and info.error else (500, "INTERNAL_ERROR", str(crash)[:1000])
    severity = severity_for(status)
    if not is_recorded(severity):
        logger.info("Respuesta %s %s en %s %s [%s]", status, code, scope.get("method"), scope.get("path"), trace_id)
        return
    error_reporter.report(
        ErrorEvent(
            source="HTTP",
            severity=severity,
            code=code,
            message=message or code,
            http_status=status,
            method=scope.get("method"),
            location=location_of(scope),
            exception_type=type(crash).__name__ if crash else None,
            detail="".join(traceback.format_exception(crash)) if crash else None,
            trace_id=trace_id,
            user_id=info.user_id,
            company_id=info.company_id,
            context=exchange.context(scope, info, elapsed_ms),
        )
    )


def register_request_id_middleware(app: FastAPI) -> None:
    app.add_middleware(RequestIdMiddleware)
