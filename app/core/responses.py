"""Contrato único de respuesta de la API.

Todas las respuestas (éxito, error, validación, 404 de rutas, middlewares, salud y el canal en vivo) tienen la forma:

    {
      "success":    bool,          # calculado a partir de statusCode (2xx → true)
      "statusCode": int,           # copia del código HTTP de la respuesta
      "code":       str,           # código estable para la lógica del cliente / traducciones
      "message":    str,           # mensaje legible para el usuario, en el idioma de la petición
      "data":       Any | null,    # objeto, colección, valor simple o null
      "errors":     [ErrorItem],   # detalles de los errores; [] cuando no existen
      "i18n": {                    # los mismos textos en CADA idioma de la API (`LOCALES`, en ese orden), o null
        "es-MX": {"message": str, "errors": [str], "texts": [str]},
        "en-US": {"message": str, "errors": [str], "texts": [str]},
        "pt-BR": {...}, "fr-FR": {...}, "de-DE": {...}, "it-IT": {...}, "es-ES": {...}
      } | null,
      "traceId":    str,           # = cabecera X-Request-ID, correlaciona con los logs
      "timestamp":  str            # ISO-8601 UTC de creación de la respuesta
    }

**`i18n`: el cambio de idioma en caliente** (regla 16 de la raíz; decisión del dueño del producto: «nunca se mezclan
idiomas; todo el idioma en caliente»). **Solo donde hace falta** (regla 3 de la raíz): en todo sobre de error (4xx/5xx)
y en lo que no se puede volver a pedir (una escritura —POST, PUT, PATCH, DELETE— y cada mensaje del canal en vivo). En
una lectura exitosa (GET de un listado o un detalle) es `null` y no se arma: la aplicación web la vuelve a pedir al
cambiar el idioma. Así su costo crece con los idiomas solo donde se usa: con N idiomas, un error típico lleva N veces su
mensaje (≈ 0.0002 MB por idioma; medido en `tests/test_envelope.py`).

Si la persona cambia el idioma mientras la aplicación web muestra un aviso con el `message` del servidor (un 409 tras un
POST, el éxito de una restauración, el error 422 de un campo), la app lo cambia al instante con `i18n[idioma]` sin
repetir una petición que no se puede repetir. `i18n[loc].errors[i]` es el texto de `errors[i].message` en ese idioma
(mismo largo y orden) y la entrada del idioma de la petición es idéntica a `message` / `errors[].message`. Nada más
cambia con el idioma (códigos, `data`, estado, cabeceras).

`i18n[loc].texts`: los textos del catálogo de mensajes que se armaron para `data` (el resultado de un registro, el
motivo de cada empleado de una operación masiva, un `StoredText`...), en ese idioma; mismo largo y orden en cada idioma
(el i-ésimo de es-MX es el español del i-ésimo de en-US), sin repetidos y a lo más `RECORDED_MAX`. Solo en lo que no se
puede volver a pedir: una petición que cambia algo (POST, PUT, PATCH, DELETE) y cada mensaje del canal en vivo, y solo
si el sobre lleva `data`; en una lectura (GET) es `[]` (la app la vuelve a pedir) y la llave siempre está. Los textos
de los catálogos de la BD dentro de `data` no van aquí (la app los vuelve a leer de `/catalogs`).

**Cómo se arma**: los constructores (`ok`, `envelope_body`, `envelope_response` y, en `app/core/exceptions.py`,
`error_response`) reciben el texto DIFERIDO (`LazyText`: un `Text` del catálogo de mensajes o una función que lo arma,
p. ej. un texto de un catálogo de la BD) y los errores como una función que los arma (`LazyErrors`); `localize` los
arma UNA vez por idioma dentro de `use_locale`, junto con los textos de `data` que anotó `t()` (`recorded_texts`).
Una cadena ya armada no se acepta (diría lo mismo en los dos idiomas): `tests/test_api_language.py` revisa la
procedencia de cada texto, en cada idioma, en todas las rutas. Armar un texto
dos veces son búsquedas en diccionarios (los catálogos de la BD salen de la instantánea de cada idioma en memoria: ni
una consulta más) y sin efectos: lo que se registra como falla (`note_error`) se anota una sola vez, en el idioma de la
petición.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import status as http_status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.core.request_context import request_id_var
from app.i18n import (
    LOCALES,
    LazyText,
    Locale,
    Params,
    Text,
    current_locale,
    is_recording,
    recorded_texts,
    recording_texts,
    render_text,
    t,
    use_locale,
)


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


class EnvelopeTexts(BaseModel):
    """Los textos del sobre en UN idioma: `message`, el de cada error (mismo orden y largo que `errors`) y los de
    `data` de una respuesta que no se puede volver a pedir (`texts`, mismo orden y largo en cada idioma)."""

    message: str = Field(description="`message` en este idioma")
    errors: list[str] = Field(default_factory=list, description="`errors[i].message` en este idioma")
    texts: list[str] = Field(
        default_factory=list,
        description="Los textos de `data` en este idioma (solo en POST, PUT, PATCH, DELETE y en el canal en vivo)",
    )


#: Los errores del sobre, por armar (en cada idioma): una función sin argumentos que los arma en el idioma vigente.
type LazyErrors = Callable[[], list[ErrorItem]]


class ApiResponse[T](BaseModel):
    success: bool = True
    statusCode: int = http_status.HTTP_200_OK
    code: str = "OK"
    message: str = Field(description="Mensaje para la persona, en el idioma de la petición (`Content-Language`)")
    data: T | None = None
    errors: list[ErrorItem] = Field(default_factory=list)
    i18n: dict[Locale, EnvelopeTexts] | None = Field(
        default=None,
        description=(
            "`message`, `errors[].message` y los textos de `data` en cada idioma de la API (cambio de idioma en "
            "caliente): en los errores, las escrituras y el canal en vivo; null en una lectura exitosa"
        ),
    )
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


@dataclass(frozen=True, slots=True)
class Localized:
    """Los textos de un sobre ya armados: los del idioma de la petición y los de cada idioma (`i18n`)."""

    message: str
    errors: list[ErrorItem]
    i18n: dict[Locale, EnvelopeTexts] | None


def localize(
    message: LazyText, errors: LazyErrors | None = None, *, data: bool = False, error: bool = False
) -> Localized:
    """Arma `message` y `errors` en el idioma de la petición y, si el sobre lleva `i18n` (un error o lo que no se
    puede volver a pedir: `is_recording`), una vez en cada idioma de la API junto con los textos que se anotaron al
    armar `data` (`texts`, sin repetidos). En una lectura exitosa solo se arma el idioma de la petición. Lo que arma
    aquí no se anota: no es de `data`."""
    every = error or is_recording()
    pairs = recorded_texts() if data else []
    rendered: dict[Locale, tuple[str, list[ErrorItem], list[str]]] = {}
    with recording_texts(enabled=False):
        for locale in LOCALES if every else (current_locale(),):
            with use_locale(locale):
                items = errors() if errors is not None else []
                rendered[locale] = (render_text(message), items, [t(key, params) for key, params in pairs])
    # Sin repetidos: el mismo texto en todos los idiomas cuenta una vez (cada idioma conserva el mismo orden).
    unique = list(dict.fromkeys(zip(*(texts for _, _, texts in rendered.values()), strict=True)))
    text, items, _ = rendered[current_locale()]
    if not every:
        return Localized(text, items, None)
    i18n = {
        locale: EnvelopeTexts(
            message=localized,
            errors=[item.message for item in localized_items],
            texts=[texts[position] for texts in unique],
        )
        for position, (locale, (localized, localized_items, _)) in enumerate(rendered.items())
    }
    return Localized(text, items, i18n)


def single_error(code: str, message: LazyText, *, field: str | None = None, details: dict | None = None) -> LazyErrors:
    """El error de siempre de un sobre de error (uno, con el texto del sobre): el cliente siempre tiene qué iterar."""
    return lambda: [ErrorItem(code=code, message=render_text(message), field=field, details=details)]


def ok(
    data: Any = None,
    message: LazyText | None = None,
    *,
    code: str = "OK",
    key: str | None = None,
    params: Params | None = None,
    status_code: int = http_status.HTTP_200_OK,
) -> ApiResponse:
    """Respuesta exitosa. `status_code` debe coincidir con el del decorador de la ruta.

    El mensaje es el del catálogo para `code` (o `key`, si el código comparte su frase con otro sentido) con sus datos
    en `params` (`{"count": result.total}` elige el plural), en el idioma de la petición y en cada idioma (`i18n`).
    `message` es el texto diferido de un resultado que le explica algo a la persona (`result.text`, un texto de un
    catálogo de la BD): nunca una cadena ya armada."""
    texts = localize(
        message if message is not None else Text(key or code, params),
        data=data is not None,
        error=not is_success(status_code),
    )
    return ApiResponse(statusCode=status_code, code=code, message=texts.message, data=data, i18n=texts.i18n)


def envelope_body(
    status_code: int,
    code: str,
    message: LazyText,
    *,
    data: Any = None,
    errors: LazyErrors | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """El sobre como diccionario JSON (manejadores de errores, middlewares, salud y el canal en vivo)."""
    texts = localize(message, errors, data=data is not None, error=not is_success(status_code))
    body = ApiResponse[Any](
        statusCode=status_code,
        code=code,
        message=texts.message,
        data=data,
        errors=texts.errors,
        i18n=texts.i18n,
        traceId=trace_id or current_trace_id(),
    )
    return jsonable_encoder(body)


def envelope_response(
    status_code: int,
    code: str,
    message: LazyText,
    *,
    data: Any = None,
    errors: LazyErrors | None = None,
    headers: dict[str, str] | None = None,
    trace_id: str | None = None,
) -> JSONResponse:
    """JSONResponse con el contrato; usado por manejadores de errores y middlewares."""
    return JSONResponse(
        status_code=status_code,
        content=envelope_body(status_code, code, message, data=data, errors=errors, trace_id=trace_id),
        headers=headers,
    )
