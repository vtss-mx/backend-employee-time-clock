"""Excepciones de dominio y manejadores globales de errores.

Todas las respuestas de error usan el contrato único de `app.core.responses`
(success=false, statusCode, code, message, data=null, errors=[...], i18n, traceId, timestamp).

**El texto de un error no se escribe al lanzarlo** (regla 16 de la raíz): `code` es el código estable y nombra su
mensaje en el catálogo (`app/i18n/messages/`); si el mismo código tiene varias frases, `key` elige la suya; los datos
van en `params`. El mensaje se arma al responder, en el idioma de la petición (y en cada idioma, `i18n`):

    raise NotFoundError(code="EMPLOYEE_NOT_FOUND")
    raise UnprocessableError(code="CURRENCY_LOCKED", params={"currency": "MXN"}, field="currency")
    raise ConflictError(code="EMPLOYEE_INACTIVE", key="EMPLOYEE_INACTIVE_ENROLL")

`message` es el texto DIFERIDO de un error cuyo texto no es un mensaje del catálogo de mensajes: uno de un catálogo de
la BD (`face_error_text(code)`, `session_text(reason)`) o el `Text` de una regla (`LocalizedValueError.text`). Nunca
una cadena ya armada: el sobre arma el texto en cada idioma (`i18n`, regla 16) y una cadena diría lo mismo en los dos
(`tests/test_i18n.py` y `tests/test_api_language.py` lo vigilan).
"""

import logging

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import note_error
from app.core.responses import ErrorItem, LazyErrors, envelope_body, single_error
from app.core.validation_errors import validation_items
from app.i18n import LazyText, Params, Text, render_text, t

logger = logging.getLogger(__name__)

#: SQLSTATE de PostgreSQL: interbloqueo y fallo de serialización (otra transacción ganó) y consulta
#: cancelada por statement_timeout.
_CONFLICT_STATES = ("40P01", "40001")
_STATEMENT_TIMEOUT = "57014"


class AppError(Exception):
    """Error de negocio o de una dependencia con su código estable y su mensaje del catálogo."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "BAD_REQUEST"

    def __init__(
        self,
        message: LazyText | None = None,
        *,
        code: str | None = None,
        key: str | None = None,
        params: Params | None = None,
        headers: dict[str, str] | None = None,
        details: dict | None = None,
        field: str | None = None,
    ) -> None:
        if code:
            self.code = code
        #: Mensaje del catálogo (por omisión, el del código) y sus datos.
        self.key = key or self.code
        self.params = params
        #: Texto diferido que no es del catálogo de mensajes (un catálogo de la BD); None = el de `key`.
        self._text = message
        super().__init__(self.key)
        self.headers = headers
        self.details = details
        #: Campo del formulario al que corresponde el error (validaciones de negocio).
        self.field = field

    @property
    def text(self) -> LazyText:
        """El mensaje diferido: el sobre lo arma en cada idioma (`i18n`)."""
        return self._text if self._text is not None else Text(self.key, self.params)

    @property
    def message(self) -> str:
        """El mensaje en el idioma vigente (el de la petición en curso)."""
        return render_text(self.text)

    def __str__(self) -> str:
        return self.message


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "UNAUTHORIZED"

    def __init__(
        self,
        message: LazyText | None = None,
        *,
        code: str | None = None,
        key: str | None = None,
        params: Params | None = None,
    ) -> None:
        super().__init__(message, code=code, key=key, params=params, headers={"WWW-Authenticate": "Bearer"})


class ApiKeyAuthenticationError(AppError):
    """401 de la API de integración: falta la llave, no existe, venció o se revocó."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "API_KEY_INVALID"

    def __init__(self, message: LazyText | None = None, *, code: str, key: str | None = None) -> None:
        super().__init__(message, code=code, key=key, headers={"WWW-Authenticate": 'ApiKey header="X-API-Key"'})


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"

    def __init__(
        self,
        message: LazyText | None = None,
        *,
        code: str | None = None,
        key: str | None = None,
        params: Params | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, key=key, params=params, details=details)


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
    sobre de siempre. Su mensaje es un `Text` del catálogo: se traduce al responder."""

    def __init__(self, message: Text) -> None:
        super().__init__(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=message)


class ServiceUnavailableError(AppError):
    """Dependencia temporalmente no disponible (BD, modelos, capacidad). El cliente puede reintentar."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "SERVICE_UNAVAILABLE"

    def __init__(
        self,
        message: LazyText | None = None,
        *,
        code: str | None = None,
        key: str | None = None,
        params: Params | None = None,
        retry_after: int = 5,
    ) -> None:
        super().__init__(message, code=code, key=key, params=params, headers={"Retry-After": str(retry_after)})


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"

    def __init__(self, retry_after: int) -> None:
        super().__init__(headers={"Retry-After": str(retry_after)})


#: Código de cada estado HTTP que solo lanza el framework (sus textos vienen en inglés, p. ej. "Missing boundary in
#: multipart."): se responde el mensaje del catálogo de ese código, en el idioma de la petición.
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


def error_response(
    status_code: int,
    code: str,
    message: LazyText,
    *,
    errors: LazyErrors | None = None,
    details: dict | None = None,
    headers: dict[str, str] | None = None,
    field: str | None = None,
) -> JSONResponse:
    """La respuesta de error con el sobre de siempre; `message` es el texto diferido (`Text("LLAVE")`): se arma en el
    idioma de la petición y en cada idioma (`i18n`)."""
    # Siempre hay al menos un elemento en `errors` para que el cliente pueda iterarlos.
    body = envelope_body(
        status_code, code, message, errors=errors or single_error(code, message, field=field, details=details)
    )
    # Toda respuesta de error pasa por aquí: se anota (una vez, con el texto que recibió la persona) para registrarla
    # en ops.error_reports si es una falla.
    note_error(status_code, code, body["message"])
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def internal_error_response() -> JSONResponse:
    return error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", Text("INTERNAL_ERROR"))


def _http_message(code: str, detail: object) -> Text:
    """El texto de un `HTTPException`: el `Text` que trae (413 al leer el cuerpo) o el del catálogo para su código
    (cada código de `_HTTP_CODES` y `HTTP_ERROR` tienen el suyo)."""
    return detail if isinstance(detail, Text) else Text(code)


def _validation_message(errors: list[ErrorItem]) -> str:
    """El `message` de un 422 de Pydantic: el del error si es uno solo; si son varios, el general."""
    return errors[0].message if len(errors) == 1 else t("VALIDATION_ERROR")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(
            exc.status_code, exc.code, exc.text, details=exc.details, headers=exc.headers, field=exc.field
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        found = exc.errors()
        return error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "VALIDATION_ERROR",
            lambda: _validation_message(validation_items(found)),
            errors=lambda: validation_items(found),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
        message = _http_message(code, exc.detail)
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
            return error_response(status.HTTP_409_CONFLICT, "CONCURRENT_UPDATE", Text("CONCURRENT_UPDATE"))
        if sqlstate == _STATEMENT_TIMEOUT:
            return error_response(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "DATABASE_TIMEOUT",
                Text("DATABASE_TIMEOUT"),
                headers={"Retry-After": "5"},
            )
        return error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "DATABASE_UNAVAILABLE",
            Text("DATABASE_UNAVAILABLE"),
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        # Dos peticiones simultáneas chocaron con una regla única de la BD (la que llegó segunda):
        # 409 reintentable en lugar de 500. La sesión se descarta al terminar la petición.
        logger.warning("Conflicto de integridad: %s", exc.orig.__class__.__name__ if exc.orig else exc)
        return error_response(status.HTTP_409_CONFLICT, "CONCURRENT_UPDATE", Text("CONCURRENT_UPDATE"))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Error no controlado: %s", exc)
        return internal_error_response()
