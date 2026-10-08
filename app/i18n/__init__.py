"""Idiomas de la API: español de México (`es-MX`, por omisión) e inglés de Estados Unidos (`en-US`).

Decisión del dueño del producto (regla 16 de la raíz): todo lo que una persona lee sale en el idioma de la petición.

- **De dónde sale el idioma** (`locale`): `Accept-Language` de cada petición (`?lang=` en el canal en vivo), resuelto
  una vez por el middleware del traceId; las respuestas JSON llevan `Content-Language` y `Vary: Accept-Language`.
- **Mensajes** (`render`, catálogo en `messages/`): `t(llave, parámetros)` o `Text` (diferido); los errores se lanzan
  con su código y sus datos, sin escribir la frase (`app/core/exceptions.py`). Lo que llega al sobre de la API es un
  `LazyText` (un `Text` o una función que arma el texto): el sobre lo arma en CADA idioma (`i18n`), así la aplicación
  web cambia de idioma un aviso abierto sin repetir la petición.
- **Catálogos de la BD**: sus textos en inglés viven en `catalog.translations` y `get_catalogs()` entrega los del
  idioma de la petición (`app/services/catalog_service.py`).
- **Textos que se guardan** (`stored`): lo que el sistema escribe en la BD para que alguien lo lea después se guarda
  como llave y datos y se traduce al leerse, en el idioma de quien lo lee.

Detalle para desarrollar: `backend-employee-time-clock/AGENTS.md`, sección "Idiomas".
"""

from app.i18n.locale import (
    DEFAULT_LOCALE,
    LOCALES,
    Locale,
    current_locale,
    locale_of,
    negotiate,
    reset_locale,
    set_locale,
    use_locale,
)
from app.i18n.render import (
    DayMonth,
    LazyText,
    LocalizedValueError,
    Megabytes,
    MissingTextError,
    Params,
    Text,
    error_text,
    format_clock,
    format_count,
    format_date,
    format_list,
    has_text,
    is_recording,
    recorded_texts,
    recording_texts,
    render_text,
    start_recording,
    stop_recording,
    strict,
    t,
)
from app.i18n.stored import StoredText, read_stored, stored

__all__ = [
    "DEFAULT_LOCALE",
    "LOCALES",
    "DayMonth",
    "LazyText",
    "Locale",
    "LocalizedValueError",
    "Megabytes",
    "MissingTextError",
    "Params",
    "StoredText",
    "Text",
    "current_locale",
    "error_text",
    "format_clock",
    "format_count",
    "format_date",
    "format_list",
    "has_text",
    "is_recording",
    "locale_of",
    "negotiate",
    "read_stored",
    "recorded_texts",
    "recording_texts",
    "render_text",
    "reset_locale",
    "set_locale",
    "start_recording",
    "stop_recording",
    "stored",
    "strict",
    "t",
    "use_locale",
]
