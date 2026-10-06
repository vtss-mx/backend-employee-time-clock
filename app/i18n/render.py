"""Textos para personas en el idioma de la petición: `t(llave, parámetros)` y `Text` (el mismo texto, diferido).

Cada texto que la API manda a una persona vive en el catálogo de mensajes (`app/i18n/messages/`), con una llave
estable, en es-MX y en en-US. El código nunca escribe la frase: nombra su llave y le pasa los datos como parámetros
(`{count}`, `{name}`...), y la frase se arma al responder, en el idioma de quien la lee. Así un error se lanza sin
escribir español (`NotFoundError(code="EMPLOYEE_NOT_FOUND")`) y cada idioma ordena la oración a su manera.

- **Parámetros**: se pasan como datos, nunca concatenados. Cada tipo se escribe como se acostumbra en el idioma:
  una fecha (`date`) como 05/10/2026 en es-MX y 10/05/2026 en en-US, `DayMonth` sin el año, una hora (`time`, ya en
  la zona del negocio) como 07:55 o 7:55 AM, un tamaño (`Megabytes`) en MB con dos decimales (regla 17), una lista
  como «A, B y C» o «A, B, and C» y un `Text` en el mismo idioma. `count` además elige la forma del plural y se
  escribe con separador de miles.
- **Plurales**: un mensaje con formas `one` / `other` (y `zero` si el cero lleva otro texto) se elige con `count`.
  Las dos lenguas usan la misma regla (1 → `one`; 0 y lo demás → `other`).
- **Llave o parámetro faltante**: nunca rompe la respuesta (se registra como error del sistema y se responde la
  llave o el marcador tal cual); las pruebas lo vuelven un fallo (`strict`), así que no llega a producción.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final

from app.i18n.locale import Locale, current_locale
from app.i18n.messages import MESSAGES, Message

logger = logging.getLogger(__name__)

type Params = Mapping[str, object]

#: Formato de las fechas en cada idioma (día, mes y año con dos dígitos como lo escribe cada país).
_DATE_FORMATS: Final[Mapping[Locale, str]] = {"es-MX": "%d/%m/%Y", "en-US": "%m/%d/%Y"}
_DAY_MONTH_FORMATS: Final[Mapping[Locale, str]] = {"es-MX": "%d/%m", "en-US": "%m/%d"}


class MissingTextError(LookupError):
    """Una llave o un parámetro que el catálogo no tiene (solo en modo estricto: las pruebas)."""


def _log_missing(problem: str) -> None:
    logger.error("Texto sin traducción: %s", problem)


def _raise_missing(problem: str) -> None:
    raise MissingTextError(problem)


#: Qué hacer con una llave o un parámetro faltante. En producción, registrarlo (error del sistema para el ADMIN) y
#: seguir; las pruebas usan `strict(True)` para que ninguno pase inadvertido.
_on_missing: Callable[[str], None] = _log_missing


def strict(enabled: bool) -> None:
    """Modo estricto (pruebas): una llave o un parámetro faltante lanza `MissingTextError` en lugar de registrarse."""
    global _on_missing
    _on_missing = _raise_missing if enabled else _log_missing


@dataclass(frozen=True, slots=True)
class DayMonth:
    """Un día del año sin el año (05/10 o 10/05): «tu siguiente turno es el 05/10»."""

    value: date


@dataclass(frozen=True, slots=True)
class Megabytes:
    """Un tamaño de datos: siempre en MB (base 1024) con dos decimales, «< 0.01 MB» si es menor (regla 17 de la
    raíz: nunca KB ni GB), igual que `formatBytes` de la aplicación web."""

    bytes: int


@dataclass(frozen=True, slots=True)
class Text:
    """Un texto diferido: su llave y sus datos. Se traduce al leerse (`str(texto)`), en el idioma vigente: sirve para
    un texto que se arma antes de saber quién lo va a leer o dentro de otro (`{reason}`)."""

    key: str
    params: Params | None = None

    def __str__(self) -> str:
        return t(self.key, self.params)


def has_text(key: str) -> bool:
    """¿El catálogo tiene esa llave? (Los idiomas tienen las mismas: lo verifica `tests/test_i18n.py`.)"""
    return key in MESSAGES[current_locale()]


def format_date(value: date, locale: Locale | None = None) -> str:
    """Una fecha como se escribe en el idioma: 05/10/2026 (es-MX) o 10/05/2026 (en-US)."""
    return value.strftime(_DATE_FORMATS[locale or current_locale()])


def format_clock(value: time, locale: Locale | None = None) -> str:
    """Una hora (ya en la zona del negocio) como la escribe la app: 07:55 / 19:05 en es-MX, 7:55 AM / 7:05 PM en
    en-US (igual que `formatTime` de la aplicación web)."""
    if (locale or current_locale()) == "es-MX":
        return f"{value.hour:02d}:{value.minute:02d}"
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def format_list(items: Sequence[object], locale: Locale | None = None) -> str:
    """Elementos unidos como se dicen: «A», «A y B», «A, B y C» en es-MX; «A and B», «A, B, and C» en en-US."""
    target = locale or current_locale()
    texts = [_value(item, target) for item in items]
    if len(texts) <= 1:
        return "".join(texts)
    key = "LIST_PAIR" if len(texts) == 2 else "LIST_SERIES"
    return t(key, {"items": ", ".join(texts[:-1]), "last": texts[-1]}, locale=target)


def _value(value: object, locale: Locale) -> str:
    """Un parámetro escrito como se acostumbra en el idioma."""
    if isinstance(value, Text):
        return t(value.key, value.params, locale=locale)
    if isinstance(value, datetime):
        return f"{format_date(value.date(), locale)} {format_clock(value.time(), locale)}"
    if isinstance(value, date):
        return format_date(value, locale)
    if isinstance(value, DayMonth):
        return value.value.strftime(_DAY_MONTH_FORMATS[locale])
    if isinstance(value, time):
        return format_clock(value, locale)
    if isinstance(value, Megabytes):
        megabytes = value.bytes / (1024 * 1024)
        return "< 0.01 MB" if 0 < megabytes < 0.005 else f"{megabytes:,.2f} MB"
    if isinstance(value, list | tuple):
        return format_list(value, locale)
    return str(value)


class _Values(dict[str, str]):
    """Los parámetros ya escritos; uno que el mensaje usa y no llegó se avisa y se deja como `{nombre}`."""

    def __init__(self, key: str, values: dict[str, str]) -> None:
        super().__init__(values)
        self.key = key

    def __missing__(self, name: str) -> str:
        _on_missing(f"{self.key}: falta el parámetro {{{name}}}")
        return "{" + name + "}"


def _form(message: Message, count: object) -> str:
    """El texto del mensaje o, si tiene plural, la forma que corresponde a `count`."""
    if isinstance(message, str):
        return message
    if count == 0 and "zero" in message:
        return message["zero"]
    return message["one" if count == 1 else "other"]


def t(key: str, params: Params | None = None, *, locale: Locale | None = None) -> str:
    """El texto de `key` en el idioma de la petición (o en `locale`) con sus parámetros."""
    target = locale or current_locale()
    message = MESSAGES[target].get(key)
    if message is None:
        _on_missing(f"{key} ({target})")
        return key
    values = {name: _value(value, target) for name, value in (params or {}).items()}
    count = (params or {}).get("count")
    if isinstance(count, int):
        values["count"] = f"{count:,}"
    return _form(message, count).format_map(_Values(key, values))


class LocalizedValueError(ValueError):
    """El `ValueError` de una regla de validación (Pydantic o en vivo) con su mensaje del catálogo: el manejador del
    422 lo traduce en el idioma de la petición (`errors[].message`) y `str(error)` también lo hace (quien lo atrapa
    para dar el resultado de una validación en vivo recibe el texto en el idioma de quien escribe). El código del
    error (`VALUE_ERROR`) no cambia."""

    def __init__(self, key: str, params: Params | None = None) -> None:
        super().__init__(key)
        self.key = key
        self.params = params

    @property
    def message(self) -> str:
        return t(self.key, self.params)

    def __str__(self) -> str:
        return self.message
