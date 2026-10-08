"""Asigna un identificador a cada petición (X-Request-ID = traceId) para correlacionar logs y respuestas,
registra en ops.error_reports la falla del servidor con que terminó (si la hubo), suma su consumo al
medidor (`usage_meter`: empresa, cuenta, ruta, bytes recibidos y enviados, tiempo y errores) y su rendimiento
(`perf_meter`: tiempo en el histograma de su ruta, tiempo y sentencias de la BD, bytes y fallas; regla 18: si tardó
más que `SLOW_REQUEST_THRESHOLD_MS` —las rutas faciales, más que `SLOW_REQUEST_FACE_THRESHOLD_MS`—, su alerta
agrupada por ruta en `slow_requests`). Todo solo en memoria, sin tocar la BD: se guarda en lotes.

Solo las fallas del servidor (5xx y excepciones no controladas) van a la bandeja del ADMIN; un 4xx es
un resultado normal y queda en el log del proceso con su código y traceId (`app/core/error_events.py`).

También resuelve el idioma de la petición (regla 16): `Accept-Language` (en el canal en vivo, `?lang=` y si no, la
cabecera) → `current_locale()` para toda la petición, y cada respuesta JSON dice en qué idioma va
(`Content-Language`) y que depende de esa cabecera (`Vary: Accept-Language`, para que ningún caché mezcle idiomas).
Se lee en la misma vuelta por las cabeceras que el X-Request-ID: cuesta microsegundos y ninguna consulta. En una
petición que cambia algo (no GET) enciende además la anotación de los textos de `data`, que el sobre arma en cada
idioma (`i18n[idioma].texts`: una respuesta así no se puede volver a pedir al cambiar de idioma).

Middleware ASGI puro: no usa BaseHTTPMiddleware (que crea tareas y streams adicionales por
petición y reduce el rendimiento con alta concurrencia).
"""

import logging
import re
import time
import traceback
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.admission import is_face_route, is_ocr_route, is_voice_route
from app.core.config import settings
from app.core.error_context import CAPTURE_LIMIT, BodyCapture, headers_of, parse_body, query_of
from app.core.error_events import ErrorEvent, is_recorded, route_of, severity_for
from app.core.exceptions import internal_error_response
from app.core.lifecycle import draining
from app.core.observability import RequestTimer, request_timer_var
from app.core.perf_meter import perf_meter, slow_requests
from app.core.request_context import RequestInfo, request_id_var, request_info_var
from app.core.responses import new_trace_id
from app.i18n import Locale, locale_of, negotiate, reset_locale, set_locale, start_recording, stop_recording
from app.services.error_reporter import error_reporter
from app.services.usage_meter import OTHER_ROUTE, usage_meter

logger = logging.getLogger("app.access")
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
#: Lo que se puede volver a pedir: sus textos de `data` no se anotan para `i18n[idioma].texts` (la app la repite).
_REPEATABLE = frozenset({"GET", "HEAD", "OPTIONS"})


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
        #: Bytes del cuerpo de la respuesta (consumo de datos de salida).
        self.sent_bytes = 0

    def received(self, message: Message) -> None:
        if message["type"] == "http.request":
            self.request.feed(message.get("body", b""))

    def sent(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            types = [v for k, v in message.get("headers", []) if k.lower() == b"content-type"]
            self.response_type = types[0].decode("latin-1") if types else ""
            return
        body = message.get("body", b"")  # el cuerpo (o una extensión sin cuerpo: cuenta 0 bytes)
        self.sent_bytes += len(body)
        if is_recorded(severity_for(self.status)) and len(self.response) < CAPTURE_LIMIT:
            self.response += body[: CAPTURE_LIMIT - len(self.response)]

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
        if scope["type"] == "websocket":
            await _metered_websocket(self.app, scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming, languages = "", None
        for key, value in scope.get("headers", []):
            if key == b"x-request-id" and not incoming:
                incoming = value.decode("latin-1")
            elif key == b"accept-language":
                languages = value.decode("latin-1")
        rid = incoming if _VALID_ID.match(incoming) else new_trace_id()
        token = request_id_var.set(rid)
        locale = negotiate(languages)
        locale_token = set_locale(locale)
        # Una petición que cambia algo no se puede repetir: los textos de su `data` viajan en cada idioma.
        texts_token = start_recording(scope.get("method") not in _REPEATABLE)
        info = RequestInfo(method=scope.get("method"), path=scope.get("path"))
        info_token = request_info_var.set(info)
        # Tiempo y sentencias de la BD de ESTA petición (los suman los eventos del motor; mismo objeto en los hilos).
        timer = RequestTimer()
        timer_token = request_timer_var.set(timer)
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
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = rid
                _declare_language(headers, locale)
                if draining():
                    # Apagado ordenado: el proxy no vuelve a usar esta conexión (abre otra, quizá a otra
                    # réplica) y nunca le toca una que el servidor esté cerrando.
                    headers["Connection"] = "close"
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
            _account(scope, info, crash, rid, exchange, elapsed, timer)
            request_timer_var.reset(timer_token)
            request_info_var.reset(info_token)
            stop_recording(texts_token)
            reset_locale(locale_token)
            request_id_var.reset(token)


def _declare_language(headers: MutableHeaders, locale: Locale) -> None:
    """Una respuesta JSON (el sobre de la API) va en el idioma de la petición y depende de `Accept-Language`. Las
    imágenes y los archivos no cambian con el idioma: sin estas cabeceras, un caché no guarda una copia por idioma."""
    if headers.get("content-type", "").startswith("application/json"):
        headers["Content-Language"] = locale
        headers.add_vary_header("Accept-Language")


def websocket_locale(scope: Scope) -> Locale:
    """El idioma del canal en vivo: `?lang=` de su URL (lo manda la aplicación web; un navegador no deja poner
    cabeceras a un WebSocket) y, si no trae uno que la API hable, `Accept-Language`."""
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    requested = locale_of(next(iter(query.get("lang", [])), None))
    if requested is not None:
        return requested
    header = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"accept-language"), None)
    return negotiate(header)


def _account(
    scope: Scope,
    info: RequestInfo,
    crash: Exception | None,
    trace_id: str,
    exchange: _Exchange,
    elapsed_ms: float,
    timer: RequestTimer,
) -> None:
    """Al terminar la petición: registra su falla (si la hubo), suma su consumo y su rendimiento. Nada de esto
    rompe jamás la respuesta (y su propio log no se reporta)."""
    try:
        _report(scope, info, crash, trace_id, exchange, elapsed_ms)
    except Exception:
        logger.exception("No se pudo registrar el error de %s %s", scope.get("method"), scope.get("path"))
    status = 500 if crash is not None else exchange.status or 500
    # La plantilla de la ruta se calcula una vez para el consumo y el rendimiento (si fallara, el rendimiento cuenta
    # la petición en OTHER).
    route = OTHER_ROUTE
    try:
        route = route_key(scope)
        _meter(info, exchange, elapsed_ms, route, status)
    except Exception:
        logger.exception("No se pudo medir el consumo de %s %s", scope.get("method"), scope.get("path"))
    try:
        _observe(scope, info, exchange, elapsed_ms, route, status, trace_id, timer)
    except Exception:
        logger.exception("No se pudo medir el rendimiento de %s %s", scope.get("method"), scope.get("path"))


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


def route_key(scope: Scope) -> str:
    """Cómo se agrupa el consumo de una petición: método y plantilla de su ruta (`GET
    /api/employees/{employee_id}`; el canal en vivo es `WS /api/ws/validation`); lo que no corresponde a
    una ruta de la API es `OTHER` (las filas quedan acotadas por las rutas, no por cada URL que alguien
    invente)."""
    if getattr(scope.get("route"), "path_format", None) is None:
        return OTHER_ROUTE
    return f"{scope.get('method', 'WS')} {location_of(scope)}"


def _message_bytes(message: Message) -> int:
    text = message.get("text")
    return len(text.encode()) if text is not None else len(message.get("bytes") or b"")


async def _metered_websocket(app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
    """El canal en vivo también cuenta en el consumo: cada mensaje del cliente es una petición (con sus
    bytes) y cada respuesta suma bytes enviados, a la empresa y la cuenta que se autenticaron en él (las
    anota el canal en el `RequestInfo` de la conexión). Solo memoria, como las peticiones HTTP; medirlo
    jamás corta el canal."""
    info = RequestInfo(method="WS", path=scope.get("path"))
    token = request_info_var.set(info)
    locale_token = set_locale(websocket_locale(scope))

    def meter(requests: int, bytes_in: int, bytes_out: int) -> None:
        try:
            usage_meter.record(
                company_id=info.company_id,
                user_id=info.user_id,
                route=route_key(scope),
                bytes_in=bytes_in,
                bytes_out=bytes_out,
                duration_ms=0.0,
                status=200,
                requests=requests,
            )
        except Exception:
            logger.exception("No se pudo medir el consumo del canal %s", scope.get("path"))

    async def receive_metered() -> Message:
        message = await receive()
        if message["type"] == "websocket.receive":
            meter(1, _message_bytes(message), 0)
        return message

    async def send_metered(message: Message) -> None:
        if message["type"] == "websocket.send":
            meter(0, 0, _message_bytes(message))
        await send(message)

    try:
        await app(scope, receive_metered, send_metered)
    finally:
        reset_locale(locale_token)
        request_info_var.reset(token)


def _meter(info: RequestInfo, exchange: _Exchange, elapsed_ms: float, route: str, status: int) -> None:
    """Suma la petición al medidor de consumo (empresa y cuenta que la hicieron, en memoria)."""
    usage_meter.record(
        company_id=info.company_id,
        user_id=info.user_id,
        route=route,
        bytes_in=exchange.request.total,
        bytes_out=exchange.sent_bytes,
        duration_ms=elapsed_ms,
        status=status,
    )


def _observe(
    scope: Scope,
    info: RequestInfo,
    exchange: _Exchange,
    elapsed_ms: float,
    route: str,
    status: int,
    trace_id: str,
    timer: RequestTimer,
) -> None:
    """Suma la petición al rendimiento de su ruta (en memoria) y, si pasó el umbral de la regla 18, a la alerta de
    peticiones lentas de su ruta con una muestra de su contexto (solo para las lentas: armarla cuesta)."""
    perf_meter.record(
        "HTTP",
        route,
        elapsed_ms,
        error=status >= 500,
        client_error=400 <= status < 500,
        db_ms=timer.db_ms,
        db_queries=timer.db_queries,
        bytes_in=exchange.request.total,
        bytes_out=exchange.sent_bytes,
    )
    threshold = slow_threshold_ms(scope, elapsed_ms)
    if threshold is None:
        return
    logger.warning("Petición lenta %s %s %.0f ms [%s]", scope.get("method"), scope.get("path"), elapsed_ms, trace_id)
    sample = {
        "method": scope.get("method"),
        "path": scope.get("path"),
        "query": query_of(scope.get("query_string", b"")),
        "status": status,
        "duration_ms": round(elapsed_ms, 1),
        "db_ms": round(timer.db_ms, 1),
        "db_queries": timer.db_queries,
        "bytes_in": exchange.request.total,
        "bytes_out": exchange.sent_bytes,
        "user": {"id": info.user_id, "role": info.user_role} if info.user_id else None,
        "company_id": info.company_id,
        "trace_id": trace_id,
    }
    slow_requests.record(route, elapsed_ms, trace_id=trace_id, status=status, threshold_ms=threshold, sample=sample)


def slow_threshold_ms(scope: Scope, elapsed_ms: float) -> int | None:
    """El umbral de la regla 18 que esta petición pasó, o None si no es lenta. Las rutas faciales
    (`admission.FACE_PREFIXES`, decisión del dueño del 2026-10-06) usan `SLOW_REQUEST_FACE_THRESHOLD_MS`; las de la
    verificación por voz (`VOICE_PREFIXES`: transcriben), `SLOW_REQUEST_VOICE_THRESHOLD_MS`; las que corren OCR al subir
    un documento (`OCR_PREFIXES`), `SLOW_REQUEST_OCR_THRESHOLD_MS`; las demás, `SLOW_REQUEST_THRESHOLD_MS`. Lo rápido
    sale con una comparación (la ruta solo se clasifica si pasó el menor)."""
    general, face = settings.SLOW_REQUEST_THRESHOLD_MS, settings.SLOW_REQUEST_FACE_THRESHOLD_MS
    voice, ocr = settings.SLOW_REQUEST_VOICE_THRESHOLD_MS, settings.SLOW_REQUEST_OCR_THRESHOLD_MS
    if elapsed_ms <= min(general, face, voice, ocr):
        return None
    method, path = scope.get("method", ""), scope.get("path", "")
    if is_voice_route(method, path):
        threshold = voice
    elif is_face_route(method, path):
        threshold = face
    elif is_ocr_route(method, path):
        threshold = ocr
    else:
        threshold = general
    return threshold if elapsed_ms > threshold else None


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
