"""Textos que el SISTEMA guarda en la base para que una persona los lea después.

Un texto que escribe una persona (el motivo que captura la empresa, la nota de un ADMIN) es un dato: se guarda y se
muestra tal cual. Pero un texto que arma el sistema (el motivo por omisión de una nueva verificación, la nota de una
suspensión automática por falta de pago) lo lee alguien más tarde, quizá en otro idioma que el de quien lo provocó
(o en segundo plano, sin petición). Por eso se guarda su LLAVE y sus datos, y se traduce al leerse, en el idioma de
quien lo lee:

    employee.face_rejection_reason = stored("REVERIFY_DEFAULT_REASON")
    ...
    face_rejection_reason: StoredText          # en el esquema de lectura: ya traducido

Forma guardada: `i18n:{"key": "...", "params": {...}}` (cabe en las columnas de texto de siempre, sin migración). Un
valor sin ese prefijo (lo que escribió una persona y lo guardado antes de esta forma) se devuelve tal cual. Los datos
admiten texto, números y fechas.
"""

import json
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Final

from pydantic import AfterValidator

from app.i18n.render import has_text, t

PREFIX: Final = "i18n:"
_DATE: Final = "$date"

type StoredValue = str | int | date


def _encode(value: StoredValue) -> object:
    return {_DATE: value.isoformat()} if isinstance(value, date) else value


def _decode(value: object) -> object:
    if isinstance(value, dict) and set(value) == {_DATE}:
        return date.fromisoformat(str(value[_DATE]))
    return value


def stored(key: str, params: Mapping[str, StoredValue] | None = None) -> str:
    """Lo que se guarda en la columna: la llave del mensaje y sus datos (no la frase)."""
    data = {"key": key, "params": {name: _encode(value) for name, value in (params or {}).items()}}
    return PREFIX + json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def read_stored(value: str | None) -> str | None:
    """El texto guardado en el idioma de quien lo lee; uno que no es de esta forma (o que ya no se reconoce), tal
    cual."""
    if value is None or not value.startswith(PREFIX):
        return value
    try:
        data = json.loads(value[len(PREFIX) :])
        key = str(data["key"])
        params = {name: _decode(item) for name, item in dict(data.get("params") or {}).items()}
    except ValueError, KeyError, TypeError:
        return value
    return t(key, params) if has_text(key) else value


#: Campo de un esquema de lectura con un texto que pudo guardar el sistema: se traduce al armar la respuesta.
StoredText = Annotated[str | None, AfterValidator(read_stored)]
