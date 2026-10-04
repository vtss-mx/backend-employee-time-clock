"""Excepciones de dominio y manejadores globales de errores.

Todas las respuestas de error usan el contrato único de `app.core.responses`
(success=false, statusCode, code, message, data=null, errors=[...], traceId, timestamp).
"""

import logging

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import note_error
from app.core.responses import ErrorItem, envelope_response

logger = logging.getLogger(__name__)

#: SQLSTATE de PostgreSQL: interbloqueo y fallo de serialización (otra transacción ganó) y consulta
#: cancelada por statement_timeout.
_CONFLICT_STATES = ("40P01", "40001")
_STATEMENT_TIMEOUT = "57014"


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "BAD_REQUEST"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
        details: dict | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.headers = headers
        self.details = details
        #: Campo del formulario al que corresponde el error (validaciones de negocio).
        self.field = field


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "UNAUTHORIZED"

    def __init__(self, message: str = "No autenticado", *, code: str | None = None) -> None:
        super().__init__(message, code=code, headers={"WWW-Authenticate": "Bearer"})


class ApiKeyAuthenticationError(AppError):
    """401 de la API de integración: falta la llave, no existe, venció o se revocó."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "API_KEY_INVALID"

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code, headers={"WWW-Authenticate": 'ApiKey header="X-API-Key"'})


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"

    def __init__(
        self,
        message: str = "No tienes permisos para realizar esta acción",
        *,
        code: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"


class UnprocessableError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "UNPROCESSABLE"


class PayloadTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "PAYLOAD_TOO_LARGE"


class BodyTooLargeError(HTTPException):
    """El cuerpo pasa del límite MIENTRAS se lee (envío por partes, sin Content-Length).

    Es un `HTTPException` de FastAPI a propósito: al leer un formulario o un JSON, FastAPI convierte
    cualquier otra excepción en un 400 genérico en inglés; esta la deja pasar y sale como 413 con el
    sobre de siempre."""

    def __init__(self, message: str) -> None:
        super().__init__(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=message)


class ServiceUnavailableError(AppError):
    """Dependencia temporalmente no disponible (BD, modelos, capacidad). El cliente puede reintentar."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "SERVICE_UNAVAILABLE"

    def __init__(self, message: str, *, code: str | None = None, retry_after: int = 5) -> None:
        super().__init__(message, code=code, headers={"Retry-After": str(retry_after)})


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "Demasiadas solicitudes. Intenta nuevamente en unos segundos.",
            headers={"Retry-After": str(retry_after)},
        )


_HTTP_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    406: "NOT_ACCEPTABLE",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "UNPROCESSABLE",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}
#: Los HTTPException con estos estados solo los lanza el framework (con textos en inglés, p. ej.
#: "Missing boundary in multipart."): se responde en español.
_HTTP_MESSAGES = {
    400: "No se pudo leer el contenido de la solicitud",
    404: "El recurso solicitado no existe",
    405: "Método HTTP no permitido para este recurso",
}
INTERNAL_ERROR_MESSAGE = "Ocurrió un error interno. Intenta nuevamente."


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    errors: list[ErrorItem] | None = None,
    details: dict | None = None,
    headers: dict[str, str] | None = None,
    field: str | None = None,
) -> JSONResponse:
    # Siempre hay al menos un elemento en `errors` para que el cliente pueda iterarlos.
    items = errors or [ErrorItem(code=code, message=message, field=field, details=details)]
    # Toda respuesta de error pasa por aquí: se anota para registrarla en ops.error_reports.
    note_error(status_code, code, message)
    return envelope_response(status_code, code, message, errors=items, headers=headers)


def internal_error_response() -> JSONResponse:
    return error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", INTERNAL_ERROR_MESSAGE)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(
            exc.status_code, exc.code, exc.message, details=exc.details, headers=exc.headers, field=exc.field
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for err in exc.errors():
            # El primer elemento dice DÓNDE venía el dato (body, query, path, header, cookie) y se omite;
            # solo ese: un campo que se llame igual (p. ej. `path` en un cuerpo JSON) conserva su nombre.
            loc = [str(part) for part in err.get("loc", [])][1:]
            message = str(err.get("msg", "Valor inválido")).removeprefix("Value error, ")
            errors.append(
                ErrorItem(code=str(err.get("type", "invalid")).upper(), message=message, field=".".join(loc) or None)
            )
        message = errors[0].message if len(errors) == 1 else "Los datos enviados no son válidos"
        return error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "VALIDATION_ERROR", message, errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
        detail = exc.detail if isinstance(exc.detail, str) else None
        # Los textos genéricos de Starlette ("Not Found") se reemplazan por mensajes en español.
        message = _HTTP_MESSAGES.get(exc.status_code) or detail or "Solicitud no procesada"
        return error_response(exc.status_code, code, message, headers=getattr(exc, "headers", None))

    @app.exception_handler(SQLAlchemyTimeoutError)
    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def _database_unavailable(_: Request, exc: Exception) -> JSONResponse:
        # BD caída, reiniciándose o saturada: 503 reintentable en lugar de 500. El SQLSTATE distingue
        # un choque entre transacciones (409, se reintenta) de una consulta que excedió su tiempo.
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        logger.error("Error de base de datos: %s (SQLSTATE %s)", exc.__class__.__name__, sqlstate or "-")
        if sqlstate in _CONFLICT_STATES:
            return error_response(
                status.HTTP_409_CONFLICT,
                "CONCURRENT_UPDATE",
                "Otra operación modificó estos datos al mismo tiempo. Intenta nuevamente.",
            )
        if sqlstate == _STATEMENT_TIMEOUT:
            return error_response(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "DATABASE_TIMEOUT",
                "La operación tardó demasiado. Intenta nuevamente en unos segundos.",
                headers={"Retry-After": "5"},
            )
        return error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "DATABASE_UNAVAILABLE",
            "El servicio no está disponible en este momento. Intenta nuevamente en unos segundos.",
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        # Dos peticiones simultáneas chocaron con una regla única de la BD (la que llegó segunda):
        # 409 reintentable en lugar de 500. La sesión se descarta al terminar la petición.
        logger.warning("Conflicto de integridad: %s", exc.orig.__class__.__name__ if exc.orig else exc)
        return error_response(
            status.HTTP_409_CONFLICT,
            "CONCURRENT_UPDATE",
            "Otra operación modificó estos datos al mismo tiempo. Intenta nuevamente.",
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Error no controlado: %s", exc)
        return internal_error_response()
