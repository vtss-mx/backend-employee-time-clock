"""El idioma de cada petición: cuál se usa, de dónde sale y cómo lo leen los servicios.

Decisión del dueño del producto (regla 16 de la raíz): la API responde en español de México (`es-MX`, por omisión)
o en inglés de Estados Unidos (`en-US`). La aplicación web manda el idioma activo en `Accept-Language` en cada
petición y en `?lang=` en el canal en vivo; el middleware del traceId lo resuelve UNA vez por petición (`negotiate`,
sin consultas a la BD) y lo deja en una variable de contexto que cualquier capa lee con `current_locale()`. Fuera de
una petición (mantenimiento, arranque, CLI) rige el idioma por omisión.

Es una `ContextVar` y no un dato de la sesión de la BD ni del proceso: cada petición (y cada hilo del threadpool, que
recibe una copia del contexto) tiene el suyo, así que dos peticiones simultáneas en idiomas distintos nunca se
mezclan, en una réplica o en N.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Final, Literal

type Locale = Literal["es-MX", "en-US"]

#: Idioma por omisión: el de un `Accept-Language` ausente, desconocido o sin ningún idioma que la API hable.
DEFAULT_LOCALE: Final[Locale] = "es-MX"
#: Idiomas que la API habla, el de omisión primero.
LOCALES: Final[tuple[Locale, ...]] = ("es-MX", "en-US")
#: Idioma base (la etiqueta sin región) → el idioma de la API que lo atiende: `es`, `es-ES` o `es-419` → es-MX.
_BY_LANGUAGE: Final[dict[str, Locale]] = {"es": "es-MX", "en": "en-US"}
#: Rangos de `Accept-Language` que se leen como máximo: un navegador manda 2-6; una cabecera enorme no cuesta más.
_MAX_RANGES: Final = 16

_locale: ContextVar[Locale] = ContextVar("locale", default=DEFAULT_LOCALE)


def current_locale() -> Locale:
    """El idioma de la petición en curso (o el de omisión fuera de una petición)."""
    return _locale.get()


def set_locale(locale: Locale) -> Token[Locale]:
    """Fija el idioma del contexto actual; el `Token` lo restaura con `reset_locale` al terminar."""
    return _locale.set(locale)


def reset_locale(token: Token[Locale]) -> None:
    _locale.reset(token)


@contextmanager
def use_locale(locale: Locale) -> Iterator[None]:
    """El bloque se ejecuta en ese idioma (pruebas, o un texto que se arma para alguien en particular)."""
    token = set_locale(locale)
    try:
        yield
    finally:
        reset_locale(token)


def locale_of(tag: str | None) -> Locale | None:
    """El idioma de la API que atiende una etiqueta BCP 47 (`es`, `es-MX`, `EN-gb`, `en_US`), o None si no habla
    ninguno de sus idiomas. Solo cuenta el idioma base: la región solo cambia la ortografía de unas palabras."""
    if not tag:
        return None
    language = tag.strip().replace("_", "-").split("-", 1)[0].lower()
    return _BY_LANGUAGE.get(language)


def _quality(params: str) -> float:
    """El peso `q` de un rango (`;q=0.8`); sin él, 1. Uno mal escrito o fuera de 0-1 descarta el rango."""
    for param in params.split(";"):
        name, _, value = param.partition("=")
        if name.strip().lower() == "q":
            try:
                weight = float(value)
            except ValueError:
                return 0.0
            return weight if 0.0 <= weight <= 1.0 else 0.0
    return 1.0


def negotiate(header: str | None) -> Locale:
    """El idioma de la respuesta según `Accept-Language` (RFC 9110 §12.5.4): gana el de mayor peso `q` que la API
    hable; con el mismo peso, el que aparece primero; `q=0` lo excluye. Si no hay cabecera o no pide ningún idioma que
    la API hable (`fr`, `*`), es-MX. Cuesta microsegundos y ninguna consulta: corre en cada petición."""
    if not header:
        return DEFAULT_LOCALE
    best: Locale | None = None
    best_weight = 0.0
    for item in header.split(",", _MAX_RANGES)[:_MAX_RANGES]:
        tag, _, params = item.partition(";")
        weight = _quality(params) if params else 1.0
        if weight <= best_weight:
            continue
        locale = locale_of(tag)
        if locale is not None:
            best, best_weight = locale, weight
    return best or DEFAULT_LOCALE
