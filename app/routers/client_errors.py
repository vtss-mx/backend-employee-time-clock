"""Fallas de la aplicación web (navegador) → "Errores del sistema" del ADMIN.

`POST /api/client-errors` es **pública a propósito**: una pantalla puede romperse antes de iniciar
sesión (en el login mismo). Si llega un `Authorization: Bearer` válido se anota quién y su empresa;
uno inválido nunca hace fallar el reporte. Como no exige sesión, todo va acotado y en este orden:

1. Límite por IP (`RATE_LIMIT_CLIENT_ERRORS_PER_MINUTE`, 429) ANTES de leer el cuerpo.
2. Cuerpo JSON de a lo más `MAX_BODY_BYTES` (413): se cuenta mientras llega y se corta al pasarse.
3. Campos y tamaños estrictos (`ClientErrorIn`, 422 `VALIDATION_ERROR`).

Responde 202: la falla se guarda con el siguiente lote del registro de errores (no espera a la BD).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.core.config import settings
from app.core.exceptions import PayloadTooLargeError
from app.core.responses import ApiResponse, ok
from app.dependencies import DbSession
from app.middleware.rate_limit import client_ip, ip_rate_limit
from app.schemas.client_error import MAX_BODY_BYTES, ClientErrorIn
from app.schemas.common import ErrorResponse
from app.services.client_error_service import ClientErrorService

router = APIRouter(prefix="/client-errors", tags=["Errores del sistema (aplicación web)"])

_TOO_LARGE = f"El reporte excede el tamaño máximo ({MAX_BODY_BYTES // 1024} KB)"
#: El cuerpo lo lee `_report_body` (no FastAPI): su esquema se documenta aquí para Swagger.
_BODY_DOC = {"required": True, "content": {"application/json": {"schema": ClientErrorIn.model_json_schema()}}}


async def _report_body(request: Request) -> ClientErrorIn:
    """El reporte, leído aquí (no por FastAPI) para que el tope sea el de esta ruta y no el general de
    la API (pensado para fotos): un Content-Length mayor se rechaza sin leer nada y un envío por partes
    se corta en cuanto lo pasa."""
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        raise PayloadTooLargeError(_TOO_LARGE)
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            raise PayloadTooLargeError(_TOO_LARGE)
    try:
        return ClientErrorIn.model_validate_json(raw)
    except ValidationError as exc:  # el mismo 422 VALIDATION_ERROR (por campo) que el resto de la API
        errors = [{**error, "loc": ("body", *error["loc"])} for error in exc.errors(include_url=False)]
        raise RequestValidationError(errors) from exc


def _bearer_token(request: Request) -> str | None:
    """El access token si viene como `Bearer` (es opcional: sin él, el reporte va sin cuenta)."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[None],
    summary="Reportar una falla de la aplicación web (público)",
    dependencies=[Depends(ip_rate_limit("client-errors", lambda: settings.RATE_LIMIT_CLIENT_ERRORS_PER_MINUTE))],
    openapi_extra={"requestBody": _BODY_DOC},
    responses={
        413: {"model": ErrorResponse, "description": "Reporte de más de 16 KB"},
        422: {"model": ErrorResponse, "description": "Campos inválidos"},
        429: {"model": ErrorResponse, "description": "Demasiados reportes desde esta IP"},
    },
)
def report_client_error(
    request: Request, report: Annotated[ClientErrorIn, Depends(_report_body)], db: DbSession
) -> ApiResponse[None]:
    ClientErrorService(db).record(report, token=_bearer_token(request), headers=request.headers, ip=client_ip(request))
    return ok(None, "Falla registrada", code="CLIENT_ERROR_RECORDED", status_code=status.HTTP_202_ACCEPTED)
