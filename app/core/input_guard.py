"""Texto que la API no acepta, en UN lugar (regla 21 del `AGENTS.md` raíz: sin inyección SQL ni fallas por la entrada).

PostgreSQL no guarda el carácter NUL (U+0000) en un texto ni en JSONB, y psycopg no puede codificar un sustituto
UTF-16 suelto (U+D800–U+DFFF: llega con un escape `\\ud800` de JSON). Sin esta regla, un `\\x00` en cualquier campo
llegaba a la base y la petición terminaba en un 500 (o el canal en vivo en «base no disponible»). Tampoco se aceptan
los demás caracteres de control C0 salvo el tabulador y los saltos de línea: ningún formulario los escribe y solo
sirven para esconder texto en un registro, un log o una exportación. Lo que se rechaza responde 422
`INVALID_CHARACTERS` con el campo (nunca se registra: es un 4xx).

La regla (`INVALID_TEXT`) se aplica en tres lugares, de afuera hacia adentro:

1. **Toda petición** (`reject_invalid_input`, dependencia de la app en `app/main.py`, antes que la autenticación y que
   cualquier otra dependencia): los parámetros de la ruta y de la query, las cabeceras y el cuerpo que FastAPI ya leyó
   (un JSON a cualquier nivel, llaves incluidas, o los campos de texto de un formulario). Una ruta nueva queda cubierta
   sin escribir nada; el cuerpo no se vuelve a leer ni a interpretar (Starlette guarda lo leído).
2. **El canal en vivo** (`app/routers/realtime.py`): cada mensaje con `invalid_field` antes de usarlo.
3. **La última barrera, en el motor de la BD** (`refuse_unstorable`, evento `before_cursor_execute` de cada motor que
   arma `app/core/database.build_engine`): un parámetro con lo que la base de verdad no puede recibir (NUL o un
   sustituto suelto), venga de donde venga —un JSON dentro de un campo de formulario, un camino nuevo que nadie
   previó—, responde el mismo 422 en lugar de un `DataError` de psycopg. Cuesta ≈0.1 µs por texto (`str.isascii` y la
   búsqueda de NUL son de C); nunca se escribe en la base ni se arma texto por ello.
"""

import email.message
import logging
import re
from collections.abc import Iterable
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from fastapi import params
from sqlalchemy import Engine, event
from starlette.datastructures import UploadFile
from starlette.requests import HTTPConnection, Request

from app.core.exceptions import UnprocessableError

logger = logging.getLogger(__name__)

#: Lo que ningún texto de la API acepta: NUL y los controles C0 salvo tabulador (\t), salto de línea (\n) y
#: retorno (\r), y los sustitutos UTF-16 sueltos (un par válido ya llega unido como un solo carácter).
INVALID_TEXT: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")
#: Lo que la BASE no puede recibir (la última barrera solo rechaza esto: lo demás lo guarda bien).
_UNSTORABLE: Final = re.compile("[\x00\ud800-\udfff]")
#: Una cabecera solo admite el tabulador como control (CR y LF la partirían; el servidor ya las rechaza).
_INVALID_HEADER: Final = re.compile(rb"[\x00-\x08\x0a-\x1f\x7f]")
#: Valores de una sentencia que no son texto ni lo contienen (la última barrera los salta sin revisarlos).
_PLAIN: Final = frozenset({int, float, bool, bytes, type(None), Decimal, datetime, date, time, timedelta, UUID})
#: Largo máximo del campo que se nombra en el error (lo escribe el cliente: una llave de JSON, un parámetro).
_FIELD_LIMIT: Final = 120


class InvalidCharactersError(UnprocessableError):
    """422 `INVALID_CHARACTERS`: un texto trae un carácter que la API no acepta; `field` dice dónde."""

    code = "INVALID_CHARACTERS"

    def __init__(self, field: str | None = None) -> None:
        super().__init__(field=field[:_FIELD_LIMIT] if field else None)


def invalid_field(value: object, path: str = "") -> str | None:
    """Dónde está el primer texto con un carácter de `INVALID_TEXT` (`"breaks.1.note"`; `""` si es el valor mismo) o
    `None` si no hay. Recorre listas y objetos a cualquier nivel; una llave inválida se nombra por su objeto."""
    if isinstance(value, str):
        return path if INVALID_TEXT.search(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            if INVALID_TEXT.search(str(key)):
                return path
            found = invalid_field(item, f"{path}.{key}" if path else str(key))
            if found is not None:
                return found
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found = invalid_field(item, f"{path}.{index}" if path else str(index))
            if found is not None:
                return found
    return None


def ensure_valid_text(value: object) -> None:
    """422 `INVALID_CHARACTERS` si algún texto de `value` (a cualquier nivel) trae un carácter que la API no acepta.
    Lo usa también quien lee un cuerpo por su cuenta (`request_body.bounded_json`)."""
    field = invalid_field(value)
    if field is not None:
        raise InvalidCharactersError(field or None)


def _is_json(content_type: str | None) -> bool:
    """La misma regla con que FastAPI decide leer el cuerpo como JSON (`application/json` o `application/*+json`)."""
    if not content_type:
        return False
    message = email.message.Message()
    message["content-type"] = content_type
    subtype = message.get_content_subtype()
    return message.get_content_maintype() == "application" and (subtype == "json" or subtype.endswith("+json"))


def _pairs_field(pairs: Iterable[tuple[str, Any]]) -> str | None:
    """El primer nombre cuyo valor de texto (o nombre de archivo) es inválido en una lista de pares (query,
    formulario); `""` si lo inválido es el nombre mismo (no se repite en el error)."""
    for name, value in pairs:
        if INVALID_TEXT.search(name):
            return ""
        text = value.filename if isinstance(value, UploadFile) else value
        if isinstance(text, str) and INVALID_TEXT.search(text):
            return name
    return None


async def _body_field(request: Request) -> str | None:
    """El campo inválido del cuerpo que FastAPI ya leyó para la ruta (nada si la ruta no declara cuerpo: la que lo lee
    por su cuenta, como el reporte de fallas con su tope de tamaño, no se lee aquí)."""
    body = getattr(request.scope.get("route"), "body_field", None)
    if body is None:
        return None
    if isinstance(body.field_info, params.Form):
        form = await request.form()  # la misma instancia que leyó FastAPI (Starlette la guarda)
        return _pairs_field(form.multi_items())
    if not _is_json(request.headers.get("content-type")) or not await request.body():
        return None
    data: object = None
    with suppress(ValueError):  # un JSON mal formado ya lo respondió FastAPI (422 `json_invalid`)
        data = await request.json()  # el mismo que leyó FastAPI
    return invalid_field(data)


def _input_field(connection: HTTPConnection) -> str | None:
    """El campo inválido de la ruta, la query o las cabeceras (lo que trae toda conexión, también el canal en vivo)."""
    path = _pairs_field(connection.path_params.items())
    if path is not None:
        return path
    query = _pairs_field(connection.query_params.multi_items())
    if query is not None:
        return query
    headers: list[tuple[bytes, bytes]] = connection.scope.get("headers", [])
    return next((name.decode("latin-1") for name, value in headers if _INVALID_HEADER.search(value)), None)


async def reject_invalid_input(connection: HTTPConnection) -> None:
    """Dependencia de TODA la API (también el canal en vivo al conectarse): 422 `INVALID_CHARACTERS` si la ruta, la
    query, una cabecera o el cuerpo traen un carácter que la API no acepta. Corre antes que cualquier otra dependencia
    (autenticación incluida): nada de eso llega a un servicio ni a la base."""
    field = _input_field(connection)
    if field is None and isinstance(connection, Request):
        field = await _body_field(connection)
    if field is not None:
        raise InvalidCharactersError(field or None)


def _unstorable(value: Any) -> bool:
    """¿Lleva el parámetro (a cualquier nivel) algo que la base no puede recibir? Los JSON de psycopg (`Json`,
    `Jsonb`) se revisan por su objeto (`obj`): su texto viajaría con el escape `\\u0000`, que JSONB también rechaza."""
    if isinstance(value, str):
        # `isascii` es O(1) en CPython: un texto ASCII solo puede traer NUL (búsqueda de C).
        return "\x00" in value if value.isascii() else _UNSTORABLE.search(value) is not None
    if type(value) in _PLAIN:  # lo más común de una sentencia: sin buscar nada
        return False
    if isinstance(value, dict):
        return any(_unstorable(key) or _unstorable(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return any(_unstorable(item) for item in value)
    wrapped = getattr(value, "obj", None)
    return wrapped is not None and _unstorable(wrapped)


def refuse_unstorable(
    _conn: Any, _cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
) -> None:
    """Evento `before_cursor_execute`: la última barrera (ver el docstring del módulo). La sentencia no se envía y la
    petición responde 422; la sesión revierte su transacción como con cualquier error. Se revisan los valores de
    Python de la sentencia (un JSON todavía es su diccionario, igual en PostgreSQL y en SQLite); en un lote
    (`executemany`), solo los del lote que va a enviarse (no todas las filas en cada lote)."""
    values = parameters if executemany else getattr(context, "compiled_parameters", None) or parameters
    if values and _unstorable(values):
        logger.warning("Parámetro que la base no puede guardar (NUL o sustituto suelto) en: %s", statement[:200])
        raise InvalidCharactersError


def guard_engine(engine: Engine) -> Engine:
    """Instala la última barrera en el motor (lo hace `build_engine` con cada motor de la API)."""
    event.listen(engine, "before_cursor_execute", refuse_unstorable)
    return engine
