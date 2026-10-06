"""Rendimiento que mide el navegador → pantalla "Rendimiento" del ADMIN.

`POST /api/telemetry/web` es **pública a propósito** (como `/api/client-errors`): la pantalla de inicio de sesión
también se mide y ahí no hay sesión. Por eso todo va acotado y en este orden:

1. Quién manda: un `Authorization: Bearer` con un token VÁLIDO (solo se verifica su firma y vigencia: cero
   consultas; no da acceso a ningún dato) o nadie. Un token vencido o inválido cuenta como anónimo: nunca un 401.
2. Límite ANTES de leer el cuerpo: por sesión con token (`RATE_LIMIT_WEB_PERF_PER_MINUTE`) o por IP sin él
   (`RATE_LIMIT_WEB_PERF_ANONYMOUS_PER_MINUTE`, estricto), 429.
3. Cuerpo JSON de a lo más 64 KB (413) con campos estrictos (`WebPerfBatch`, 422) y a lo más
   `PERF_WEB_MAX_SAMPLES` muestras (422).
4. Sin sesión solo se toma en cuenta la pantalla de inicio de sesión y sus APIs (`web_performance`).

Responde 202: lo medido se suma en memoria y se guarda con el siguiente lote (no espera a la BD).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.core.config import settings
from app.core.exceptions import AuthenticationError
from app.core.request_body import bounded_json
from app.core.responses import ApiResponse, ok
from app.core.tokens import decode_access_token
from app.middleware.rate_limit import client_ip, enforce
from app.schemas.common import ErrorResponse
from app.schemas.performance import MAX_WEB_BODY_BYTES, WebPerfBatch, WebPerfResult
from app.services import web_performance

router = APIRouter(prefix="/telemetry", tags=["Rendimiento (aplicación web)"])

_BODY_DOC = {"required": True, "content": {"application/json": {"schema": WebPerfBatch.model_json_schema()}}}


def _session_of(request: Request) -> str | None:
    """La sesión del token si viene y es válido (firma y vigencia, sin consultas); None en cualquier otro caso."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        claims = decode_access_token(token.strip())
    except AuthenticationError:
        return None
    return str(claims.get("sid") or claims["sub"])


def _sender(request: Request) -> bool:
    """¿Lo manda una sesión? Aplica su límite (por sesión, o por IP sin sesión) antes de leer el cuerpo."""
    session = _session_of(request)
    if session is not None:
        enforce(f"web-perf:session:{session}", settings.RATE_LIMIT_WEB_PERF_PER_MINUTE)
        return True
    enforce(f"web-perf:ip:{client_ip(request)}", settings.RATE_LIMIT_WEB_PERF_ANONYMOUS_PER_MINUTE)
    return False


async def _batch(request: Request) -> WebPerfBatch:
    return await bounded_json(request, WebPerfBatch, MAX_WEB_BODY_BYTES)


@router.post(
    "/web",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[WebPerfResult],
    summary="Registrar lo que midió el navegador: Web Vitals, tareas largas y sus peticiones (público)",
    openapi_extra={"requestBody": _BODY_DOC},
    responses={
        413: {"model": ErrorResponse, "description": "Lote de más de 64 KB"},
        422: {"model": ErrorResponse, "description": "Campos inválidos o demasiadas muestras"},
        429: {"model": ErrorResponse, "description": "Demasiados lotes de esta sesión o IP"},
    },
)
def record_web_performance(
    request: Request,
    authenticated: Annotated[bool, Depends(_sender)],
    batch: Annotated[WebPerfBatch, Depends(_batch)],
) -> ApiResponse[WebPerfResult]:
    # El índice de las rutas de la API lo arma `app/main.py` una vez (las rutas no cambian).
    index: web_performance.RouteIndex = request.app.state.perf_route_index
    result = web_performance.record(batch, authenticated=authenticated, index=index)
    return ok(result, code="WEB_PERFORMANCE_RECORDED", status_code=status.HTTP_202_ACCEPTED)
