"""Contrato único de respuesta de la API.

Todas las respuestas (éxito, error, validación, 404 de rutas, middlewares, salud) tienen la forma:

    {
      "success":    bool,          # calculado a partir de statusCode (2xx → true)
      "statusCode": int,           # copia del código HTTP de la respuesta
      "code":       str,           # código estable para la lógica del cliente / traducciones
      "message":    str,           # mensaje legible para el usuario
      "data":       Any | null,    # objeto, colección, valor simple o null
      "errors":     [ErrorItem],   # detalles de los errores; [] cuando no existen
      "traceId":    str,           # = cabecera X-Request-ID, correlaciona con los logs
      "timestamp":  str            # ISO-8601 UTC de creación de la respuesta
    }
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import status as http_status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.core.request_context import request_id_var


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str:
    return request_id_var.get() or new_trace_id()


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_success(status_code: int) -> bool:
    return 200 <= status_code < 300


class ErrorItem(BaseModel):
    """Detalle de un error. `field` identifica el campo (validación); `details` datos extra."""

    code: str = Field(description="Código estable del error")
    message: str = Field(description="Mensaje legible")
    field: str | None = Field(default=None, description="Campo asociado (errores de validación)")
    details: dict[str, Any] | None = Field(default=None, description="Información adicional")


class ApiResponse[T](BaseModel):
    success: bool = True
    statusCode: int = http_status.HTTP_200_OK
    code: str = "OK"
    message: str = "Operación exitosa"
    data: T | None = None
    errors: list[ErrorItem] = Field(default_factory=list)
    traceId: str = Field(default_factory=current_trace_id)
    timestamp: str = Field(default_factory=utc_timestamp)

    @model_validator(mode="before")
    @classmethod
    def _derive_success(cls, values: Any) -> Any:
        # `success` nunca se acepta del llamador: siempre se deriva de statusCode.
        if isinstance(values, dict):
            values = dict(values)
            values["success"] = is_success(int(values.get("statusCode", http_status.HTTP_200_OK)))
        return values


def ok(
    data: Any = None,
    message: str = "Operación exitosa",
    *,
    code: str = "OK",
    status_code: int = http_status.HTTP_200_OK,
) -> ApiResponse:
    """Respuesta exitosa. `status_code` debe coincidir con el del decorador de la ruta."""
    return ApiResponse(statusCode=status_code, code=code, message=message, data=data)


def envelope_body(
    status_code: int,
    code: str,
    message: str,
    *,
    data: Any = None,
    errors: list[ErrorItem] | list[dict] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    body = ApiResponse[Any](
        statusCode=status_code,
        code=code,
        message=message,
        data=data,
        errors=errors or [],
        traceId=trace_id or current_trace_id(),
    )
    return jsonable_encoder(body)


def envelope_response(
    status_code: int,
    code: str,
    message: str,
    *,
    data: Any = None,
    errors: list[ErrorItem] | list[dict] | None = None,
    headers: dict[str, str] | None = None,
    trace_id: str | None = None,
) -> JSONResponse:
    """JSONResponse con el contrato; usado por manejadores de errores y middlewares."""
    return JSONResponse(
        status_code=status_code,
        content=envelope_body(status_code, code, message, data=data, errors=errors, trace_id=trace_id),
        headers=headers,
    )
