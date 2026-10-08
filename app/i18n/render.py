"""Textos para personas en el idioma de la petición: `t(llave, parámetros)` y `Text` (el mismo texto, diferido).

Cada texto que la API manda a una persona vive en el catálogo de mensajes (`app/i18n/messages/`), con una llave
estable, en cada uno de los idiomas de `LOCALES` (es-MX, en-US, pt-BR, fr-FR, de-DE, it-IT y es-ES). El código nunca
escribe la frase: nombra su llave y le pasa los datos como parámetros (`{count}`, `{name}`...), y la frase se arma al
responder, en el idioma de quien la lee. Así un error se lanza sin
escribir español (`NotFoundError(code="EMPLOYEE_NOT_FOUND")`) y cada idioma ordena la oración a su manera.

- **Parámetros**: se pasan como datos, nunca concatenados. Cada tipo se escribe como se acostumbra en el idioma:
  una fecha (`date`) como 05/10/2026 (es, pt, fr, it), 10/05/2026 (en-US) o 05.10.2026 (de-DE), `DayMonth` sin el
  año, una hora (`time`, ya en la zona del negocio) como 14:05 (7:55 AM solo en en-US), un tamaño (`Megabytes`) en MB
  con dos decimales (regla 17), una lista como «A, B y C», «A, B, and C», «A, B et C»… y un `Text` en el mismo idioma.
  `count` además elige la forma del plural y se escribe con el separador de miles del idioma (`format_count`).
- **Plurales**: un mensaje con formas `one` / `other` (y `zero` si el cero lleva otro texto) se elige con `count`
  con la regla de cada idioma (CLDR, la misma que `Intl.PluralRules` en la app): 1 → `one`; en pt-BR y fr-FR también
  0 → `one`; lo demás → `other`.
- **Llave o parámetro faltante**: nunca rompe la respuesta (se registra como error del sistema y se responde la
  llave o el marcador tal cual); las pruebas lo vuelven un fallo (`strict`), así que no llega a producción.
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final

from app.i18n.locale import Locale, current_locale, use_locale
from app.i18n.messages import MESSAGES, Message

logger = logging.getLogger(__name__)

type Params = Mapping[str, object]

#: Formato de las fechas en cada idioma (día, mes y año con dos dígitos como lo escribe cada país; los mismos que
#: `Intl.DateTimeFormat` en la aplicación web): 05/10/2026 · 10/05/2026 · 05.10.2026 (de-DE).
_DATE_FORMATS: Final[Mapping[Locale, str]] = {
    "es-MX": "%d/%m/%Y",
    "en-US": "%m/%d/%Y",
    "pt-BR": "%d/%m/%Y",
    "fr-FR": "%d/%m/%Y",
    "de-DE": "%d.%m.%Y",
    "it-IT": "%d/%m/%Y",
    "es-ES": "%d/%m/%Y",
}
_DAY_MONTH_FORMATS: Final[Mapping[Locale, str]] = {
    "es-MX": "%d/%m",
    "en-US": "%m/%d",
    "pt-BR": "%d/%m",
    "fr-FR": "%d/%m",
    "de-DE": "%d.%m.",
    "it-IT": "%d/%m",
    "es-ES": "%d/%m",
}
#: Los idiomas que leen la hora en un reloj de 12 horas (2:05 PM); los demás, 24 (14:05), como `formatTime` en la app.
_TWELVE_HOUR_LOCALES: Final[frozenset[str]] = frozenset({"en-US"})
#: Separador de miles y desde cuántos dígitos se agrupa, como `Intl.NumberFormat` del idioma en la aplicación web
#: (CLDR: el italiano y el español de España no agrupan una cifra de cuatro dígitos; el francés usa el espacio fino).
_GROUPING: Final[Mapping[Locale, tuple[str, int]]] = {
    "es-MX": (",", 4),
    "en-US": (",", 4),
    "pt-BR": (".", 4),
    "fr-FR": ("\u202f", 4),
    "de-DE": (".", 4),
    "it-IT": (".", 5),
    "es-ES": (".", 5),
}
#: Idiomas donde el cero toma la forma `one` del plural (CLDR: «0 dia», «0 jour»), como `Intl.PluralRules` en la app.
_ZERO_IS_ONE: Final[frozenset[str]] = frozenset({"pt-BR", "fr-FR"})


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


#: Un texto que se puede volver a armar en CUALQUIER idioma (regla 16: nunca se mezclan idiomas y el cambio es en
#: caliente): un `Text` del catálogo de mensajes o una función sin argumentos que lo arma en el idioma vigente (un texto
#: de un catálogo de la BD: `lambda: get_catalogs().reason_message(code)`, servido de la caché sin consultas). Es lo que
#: reciben el sobre de la API (`message`, `errors[].message` y su `i18n`), los errores y los resultados que le explican
#: algo a la persona: el sobre lo arma en cada idioma, así la aplicación web cambia de idioma un aviso abierto sin
#: repetir la petición. Una cadena ya armada no sirve: diría lo mismo en los dos idiomas.
type LazyText = Text | Callable[[], str]


def render_text(text: LazyText) -> str:
    """El texto diferido en el idioma vigente (el de la petición, o el de `use_locale`). Una cadena ya armada (no debe
    llegar: el tipo no la admite) se devuelve tal cual en lugar de romper la respuesta (regla 7: un 503 nunca se vuelve
    un 500); `tests/test_api_language.py` la señalaría por decir lo mismo en todos los idiomas."""
    if isinstance(text, Text):
        return str(text)
    return text() if callable(text) else str(text)


def has_text(key: str) -> bool:
    """¿El catálogo tiene esa llave? (Los idiomas tienen las mismas: lo verifica `tests/test_i18n.py`.)"""
    return key in MESSAGES[current_locale()]


def format_date(value: date, locale: Locale | None = None) -> str:
    """Una fecha como se escribe en el idioma: 05/10/2026 (es-MX) o 10/05/2026 (en-US)."""
    return value.strftime(_DATE_FORMATS[locale or current_locale()])


def format_clock(value: time, locale: Locale | None = None) -> str:
    """Una hora (ya en la zona del negocio) como la escribe la app: 07:55 / 19:05 en todos los idiomas salvo en-US,
    7:55 AM / 7:05 PM (igual que `formatTime` de la aplicación web)."""
    if (locale or current_locale()) not in _TWELVE_HOUR_LOCALES:
        return f"{value.hour:02d}:{value.minute:02d}"
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def format_count(count: int, locale: Locale | None = None) -> str:
    """Un conteo con el separador de miles del idioma: 1,234 (es-MX, en-US), 1.234 (pt-BR, de-DE), 1 234 (fr-FR) y
    1234 pero 12.345 (it-IT, es-ES)."""
    separator, min_digits = _GROUPING[locale or current_locale()]
    if len(str(abs(count))) < min_digits:
        return str(count)
    return f"{count:,}".replace(",", separator)


def format_list(items: Sequence[object], locale: Locale | None = None) -> str:
    """Elementos unidos como se dicen en cada idioma con sus llaves `LIST_PAIR` y `LIST_SERIES`: «A, B y C» (es), «A, B,
    and C» (en-US), «A, B e C» (pt, it), «A, B et C» (fr), «A, B und C» (de)."""
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
    if callable(value):  # un texto diferido de un catálogo de la BD (`LazyText`): en el idioma del mensaje que lo lleva
        with use_locale(locale):
            return str(value())
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


def _form(message: Message, count: object, locale: Locale) -> str:
    """El texto del mensaje o, si tiene plural, la forma que corresponde a `count` en ese idioma (en pt-BR y fr-FR el
    cero también es `one`; `zero`, si existe, gana en todos)."""
    if isinstance(message, str):
        return message
    if count == 0 and "zero" in message:
        return message["zero"]
    one = count == 1 or (count == 0 and locale in _ZERO_IS_ONE)
    return message["one" if one else "other"]


#: Los textos de `data` de una respuesta que no se puede volver a pedir (regla 16, cambio de idioma en caliente): en una
#: petición que cambia algo (POST, PUT, PATCH, DELETE) y en cada mensaje del canal en vivo se anota la llave y los datos
#: de cada `t()` de primer nivel (el resultado de un registro, el motivo de una operación masiva, un `StoredText`...) y
#: el sobre los arma en cada idioma (`i18n[idioma].texts`): la aplicación web cambia esos textos sin repetir la
#: petición. En una lectura (GET) está apagado (la app la vuelve a pedir) y no cuesta nada. Acotado a `RECORDED_MAX`.
_recorded: ContextVar[list[tuple[str, Params | None]] | None] = ContextVar("recorded_texts", default=None)
RECORDED_MAX: Final = 200


def start_recording(enabled: bool = True) -> Token[list[tuple[str, Params | None]] | None]:
    """Empieza (o apaga, `enabled=False`) la anotación de los textos de esta petición o de este mensaje."""
    return _recorded.set([] if enabled else None)


def stop_recording(token: Token[list[tuple[str, Params | None]] | None]) -> None:
    _recorded.reset(token)


@contextmanager
def recording_texts(enabled: bool = True) -> Iterator[None]:
    """El bloque anota sus textos (`enabled`) o ninguno (al armar el sobre: sus textos no son de `data`)."""
    token = start_recording(enabled)
    try:
        yield
    finally:
        stop_recording(token)


def is_recording() -> bool:
    """¿Se anotan los textos? Solo en lo que no se puede volver a pedir (una escritura o un mensaje del canal en
    vivo): ahí el sobre lleva sus textos en cada idioma (`i18n`); una lectura se vuelve a pedir al cambiar el idioma."""
    return _recorded.get() is not None


def recorded_texts() -> list[tuple[str, Params | None]]:
    """La llave y los datos de cada texto anotado hasta ahora (en orden; vacío si no se anota)."""
    return list(_recorded.get() or ())


def t(key: str, params: Params | None = None, *, locale: Locale | None = None) -> str:
    """El texto de `key` en el idioma de la petición (o en `locale`) con sus parámetros."""
    target = locale or current_locale()
    message = MESSAGES[target].get(key)
    if message is None:
        _on_missing(f"{key} ({target})")
        return key
    recorder = _recorded.get()
    if recorder is not None and len(recorder) < RECORDED_MAX:
        recorder.append((key, params))
    values: dict[str, str] = {}
    if params:
        nested = _recorded.set(None)  # los textos de sus parámetros van dentro de este, no aparte
        try:
            values = {name: _value(value, target) for name, value in params.items()}
        finally:
            _recorded.reset(nested)
    count = (params or {}).get("count")
    if isinstance(count, int):
        values["count"] = format_count(count, target)
    return _form(message, count, target).format_map(_Values(key, values))


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

    @property
    def text(self) -> Text:
        """El mismo mensaje diferido (para un error o un resultado que se arma en cada idioma)."""
        return Text(self.key, self.params)

    def __str__(self) -> str:
        return self.message


def error_text(error: ValueError) -> Text:
    """El mensaje (diferido) de una regla de validación que no se cumplió: el suyo si es una `LocalizedValueError`; si
    la lanzó otra pieza (una librería, con su texto en inglés), el genérico del catálogo. Nunca el texto de la
    excepción: estaría en un solo idioma."""
    return error.text if isinstance(error, LocalizedValueError) else Text("INPUT_INVALID")
