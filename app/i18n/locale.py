"""El idioma de cada petición: cuál se usa, de dónde sale y cómo lo leen los servicios.

Decisión del dueño del producto (regla 16 de la raíz, 2026-10-06): la API responde en siete idiomas: español de México
(`es-MX`, por omisión), inglés de Estados Unidos (`en-US`), portugués de Brasil (`pt-BR`), francés (`fr-FR`), alemán
(`de-DE`), italiano (`it-IT`) y español de España (`es-ES`); registro y glosario en `docs/i18n/glosario.md`. La
aplicación web manda el idioma activo en `Accept-Language` en cada
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

#: Los siete idiomas de la plataforma (`docs/i18n/glosario.md`); `LOCALES` dice cuáles habla ya esta versión.
type Locale = Literal["es-MX", "en-US", "pt-BR", "fr-FR", "de-DE", "it-IT", "es-ES"]

#: Idioma por omisión: el de un `Accept-Language` ausente, desconocido o sin ningún idioma que la API hable.
DEFAULT_LOCALE: Final[Locale] = "es-MX"
#: Idiomas que la API habla, el de omisión primero (el mismo orden que `LOCALES` de la aplicación web).
LOCALES: Final[tuple[Locale, ...]] = ("es-MX", "en-US", "pt-BR", "fr-FR", "de-DE", "it-IT", "es-ES")
#: Idioma base (la etiqueta sin región) → el idioma de la API que lo atiende: `es`, `es-419` o `es-AR` → es-MX (el
#: español sin región y el de Hispanoamérica comparten vocabulario con México y es-MX es el idioma por omisión);
#: `pt-PT` → pt-BR (solo hay portugués de Brasil).
_BY_LANGUAGE: Final[dict[str, Locale]] = {
    "es": "es-MX",
    "en": "en-US",
    "pt": "pt-BR",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
}
#: Regiones que cambian el idioma: el español de España —también Ceuta y Melilla (`EA`) y Canarias (`IC`)— → es-ES.
_BY_REGION: Final[dict[str, Locale]] = {"es-es": "es-ES", "es-ea": "es-ES", "es-ic": "es-ES"}
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
    """El idioma de la API que atiende una etiqueta BCP 47 (`es`, `es-MX`, `EN-gb`, `en_US`, `pt-PT`, `es-ES`), o None
    si no habla ninguno de sus idiomas. Cuenta el idioma base y, solo para el español de España, la región
    (`docs/i18n/glosario.md` §5)."""
    if not tag:
        return None
    language, _, rest = tag.strip().replace("_", "-").lower().partition("-")
    region = rest.split("-", 1)[0]
    return _BY_REGION.get(f"{language}-{region}") or _BY_LANGUAGE.get(language)


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
    la API hable (`ja`, `*`), es-MX. Cuesta microsegundos y ninguna consulta: corre en cada petición."""
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
