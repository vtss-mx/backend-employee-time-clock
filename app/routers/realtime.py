"""Canal WebSocket de validación en tiempo real de los formularios (todos los roles).

Protocolo (JSON). Todas las respuestas usan el contrato único de la API (success, statusCode,
code, message, data, errors, traceId, timestamp); `traceId` es el `id` enviado por el cliente
para correlacionar pregunta y respuesta.

    → {"type": "auth", "token": "<access token JWT>"}            (primer mensaje, ≤ 10 s)
    ← code WS_AUTHENTICATED

    → {"type": "validate", "id": "a1b2c3d4", "field": "phone", "value": "+526621234567",
       "excludeId": 7, "related": "ana@x.com"}     (excludeId al editar; related: el correo
                                                    escrito, al validar el teléfono de un alta)
    ← code AVAILABLE | LINKABLE | TAKEN | MISMATCH | VALID | INVALID_FORMAT | EMPTY, data = resultado
      (403 FIELD_NOT_ALLOWED si el rol no tiene la pantalla que usa el campo)

Los campos y sus permisos viven en app/services/live_validation.py (los mismos que el respaldo
HTTP `GET /api/validation`); al autenticarse, el canal informa los campos del usuario.

    → {"type": "ping"}                                              ← code PONG

Cada validación usa una conexión de la BD: como mucho WS_MAX_CONCURRENT_VALIDATIONS a la vez por
proceso (las demás esperan su turno), para que cientos de formularios abiertos no dejen sin
conexiones a la API HTTP.

El token NO viaja en la URL (quedaría en logs de proxies). Cierres: 4401 sin autenticar o
sesión revocada/expirada, 4403 rol sin campos que validar, 4408 no se autenticó a tiempo, 1013 servidor
saturado, 1000 inactividad.
"""

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.error_events import ErrorEvent, severity_for
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.responses import envelope_body, new_trace_id
from app.core.tokens import decode_access_token
from app.services.error_reporter import error_reporter
from app.services.live_validation import fields_for, validate_field
from app.services.session_service import SessionService

router = APIRouter(tags=["Tiempo real"])
logger = logging.getLogger(__name__)

_TRACE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_open_connections = 0
#: Validaciones del canal en curso (cada una ocupa un hilo y una conexión de la BD).
_validation_slots = asyncio.Semaphore(settings.WS_MAX_CONCURRENT_VALIDATIONS)


@dataclass
class _Client:
    user_id: int
    session_id: str
    expires_at: float
    #: Campos que el usuario puede validar (según las pantallas de su rol).
    fields: frozenset[str]


class _Closed(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _envelope(
    status: int, code: str, message: str, *, data: Any = None, trace_id: str | None = None, report: bool = True
) -> dict:
    """Mensaje del canal con el contrato de siempre. Los errores del canal quedan registrados para el
    ADMIN; el resultado de una validación (`report=False`: «ese correo ya existe» mientras se escribe)
    es una respuesta normal, no una falla."""
    trace = trace_id or new_trace_id()
    errors = [] if 200 <= status < 300 else [{"code": code, "message": message, "field": None, "details": None}]
    if status >= 400 and report:
        error_reporter.report(
            ErrorEvent(
                source="WEBSOCKET",
                severity=severity_for(status),
                code=code,
                message=message,
                http_status=status,
                location="/api/ws/validation",
                trace_id=trace,
            )
        )
    return envelope_body(status, code, message, data=data, errors=errors, trace_id=trace)


def _authenticate(token: str, headers: Mapping[str, str]) -> _Client:
    """Las mismas reglas que una petición HTTP; además, el usuario debe tener algún campo que validar."""
    with SessionLocal() as db:
        user, session_id = SessionService(db).authenticate_access(token, headers)
        fields = frozenset(fields_for(user))
        if not fields:
            raise PermissionError
    expires_at = float(decode_access_token(token)["exp"])
    return _Client(user_id=user.id, session_id=session_id, expires_at=expires_at, fields=fields)


def _validate(client: _Client, field: str, value: str, exclude_id: int | None, related: str | None) -> dict[str, Any]:
    with SessionLocal() as db:
        user = SessionService(db).active_user(client.session_id, client.user_id)  # cerrar sesión corta el canal
        return validate_field(db, user, field, value, exclude_id, related).as_dict()


class _RateLimiter:
    def __init__(self, limit: int, window: float = 10.0) -> None:
        self.limit, self.window, self.hits = limit, window, deque[float]()

    def allow(self) -> bool:
        now = time.monotonic()
        while self.hits and self.hits[0] <= now - self.window:
            self.hits.popleft()
        if len(self.hits) >= self.limit:
            return False
        self.hits.append(now)
        return True


async def _receive_json(ws: WebSocket, timeout: float) -> dict[str, Any] | None:
    """Siguiente mensaje JSON (None si no es un objeto JSON válido o excede el tamaño)."""
    try:
        text = await asyncio.wait_for(ws.receive_text(), timeout=timeout)
    except TimeoutError as exc:
        raise _Closed(1000) from exc
    if len(text.encode()) > settings.WS_MAX_MESSAGE_BYTES:
        return None
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return None
    return message if isinstance(message, dict) else None


_DB_DOWN = "El servicio no está disponible en este momento. Intenta nuevamente en unos segundos."


async def _handshake(ws: WebSocket) -> _Client:
    try:
        message = await _receive_json(ws, settings.WS_AUTH_TIMEOUT_SECONDS)
    except _Closed as exc:
        await ws.send_json(_envelope(401, "WS_AUTH_TIMEOUT", "No se recibió la autenticación a tiempo"))
        raise _Closed(4408) from exc
    token = message.get("token") if message and message.get("type") == "auth" else None
    if not isinstance(token, str) or not token:
        await ws.send_json(_envelope(401, "UNAUTHORIZED", 'El primer mensaje debe ser {"type": "auth", "token": ...}'))
        raise _Closed(4401)
    try:
        # Con el mismo tope que las validaciones: mil reconexiones a la vez no agotan hilos ni el pool.
        async with _validation_slots:
            client = await run_in_threadpool(_authenticate, token, ws.headers)
    except SQLAlchemyError as exc:
        await ws.send_json(_envelope(503, "DATABASE_UNAVAILABLE", _DB_DOWN))
        raise _Closed(1013) from exc  # 1013: inténtalo más tarde
    except AuthenticationError as exc:
        await ws.send_json(_envelope(401, exc.code, exc.message))
        raise _Closed(4401) from exc
    except PermissionDeniedError as exc:  # p. ej. un validador desde un dispositivo no permitido
        await ws.send_json(_envelope(403, exc.code, exc.message))
        raise _Closed(4403) from exc
    except PermissionError as exc:
        await ws.send_json(_envelope(403, "FORBIDDEN", "Tu cuenta no tiene campos que validar en este canal"))
        raise _Closed(4403) from exc
    await ws.send_json(
        _envelope(200, "WS_AUTHENTICATED", "Canal de validación listo", data={"fields": sorted(client.fields)})
    )
    return client


async def _handle(ws: WebSocket, client: _Client, message: dict[str, Any] | None, limiter: _RateLimiter) -> None:
    trace = message.get("id") if message else None
    trace_id = trace if isinstance(trace, str) and _TRACE_ID.match(trace) else None
    if message is None:
        await ws.send_json(_envelope(400, "BAD_MESSAGE", "Mensaje inválido (JSON de máx. 4 KB)", trace_id=trace_id))
        return
    if time.time() >= client.expires_at:
        await ws.send_json(_envelope(401, "TOKEN_EXPIRED", "La sesión ha expirado", trace_id=trace_id))
        raise _Closed(4401)
    if not limiter.allow():
        await ws.send_json(
            _envelope(429, "RATE_LIMITED", "Demasiadas validaciones; espera un momento", trace_id=trace_id)
        )
        return
    kind = message.get("type")
    if kind == "ping":
        await ws.send_json(_envelope(200, "PONG", "pong", trace_id=trace_id))
        return
    field, value, exclude = message.get("field"), message.get("value"), message.get("excludeId")
    if kind != "validate" or not isinstance(field, str) or not isinstance(value, str) or len(value) > 255:
        await ws.send_json(_envelope(400, "BAD_MESSAGE", "Mensaje de validación inválido", trace_id=trace_id))
        return
    if field not in client.fields:
        await ws.send_json(_envelope(403, "FIELD_NOT_ALLOWED", "No puedes validar este campo", trace_id=trace_id))
        return
    exclude_id = exclude if isinstance(exclude, int) and not isinstance(exclude, bool) else None
    related = message.get("related")
    related_value = related[:255] if isinstance(related, str) else None
    try:
        async with _validation_slots:
            result = await run_in_threadpool(_validate, client, field, value, exclude_id, related_value)
    except AuthenticationError as exc:
        await ws.send_json(_envelope(401, exc.code, exc.message, trace_id=trace_id))
        raise _Closed(4401) from exc
    except PermissionDeniedError as exc:  # la pantalla se le retiró al rol con el canal abierto
        await ws.send_json(_envelope(403, exc.code, exc.message, trace_id=trace_id))
        return
    except SQLAlchemyError:  # parpadeo de la BD: esa validación falla, el canal sigue abierto
        logger.warning("Validación en vivo sin base de datos", exc_info=True)
        await ws.send_json(_envelope(503, "DATABASE_UNAVAILABLE", _DB_DOWN, trace_id=trace_id))
        return
    status = 200 if result["valid"] else 422
    await ws.send_json(
        _envelope(status, result["code"], result["message"], data=result, trace_id=trace_id, report=False)
    )


@router.websocket("/ws/validation")
async def validation_socket(ws: WebSocket) -> None:
    global _open_connections
    await ws.accept()
    if _open_connections >= settings.WS_MAX_CONNECTIONS:
        await ws.send_json(_envelope(503, "SERVER_BUSY", "Demasiadas conexiones; intenta en unos segundos"))
        await ws.close(code=1013)
        return
    _open_connections += 1
    try:
        client = await _handshake(ws)
        limiter = _RateLimiter(settings.WS_MAX_MESSAGES_PER_10S)
        while True:
            message = await _receive_json(ws, settings.WS_IDLE_TIMEOUT_SECONDS)
            await _handle(ws, client, message, limiter)
    except _Closed as closed:
        await ws.close(code=closed.code)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Error en el canal de validación")
        await ws.close(code=1011)
    finally:
        _open_connections -= 1
