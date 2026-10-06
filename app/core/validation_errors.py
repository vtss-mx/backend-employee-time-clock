"""Los errores de validación de Pydantic (422) con su mensaje en el idioma de la petición.

Pydantic escribe sus mensajes en inglés ("String should have at least 2 characters") y una regla propia lanza su
`ValueError`. Aquí cada error se vuelve un `ErrorItem` con:

- `code`: el tipo de Pydantic en mayúsculas (`MISSING`, `STRING_TOO_SHORT`, `VALUE_ERROR`...), igual que siempre;
- `field`: la ruta del campo sin el lugar de donde vino (`body`, `query`...);
- `message`: el de una regla propia (`LocalizedValueError`, con su llave del catálogo) o el del catálogo para el tipo
  de Pydantic (`INPUT_*`, con el límite que trae el error: mínimo de caracteres, máximo, etc.). Un tipo que no está en
  la tabla responde el genérico `INPUT_INVALID`: nunca el texto en inglés de Pydantic.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.core.responses import ErrorItem
from app.i18n import LocalizedValueError, t

#: Tipo de error de Pydantic → (llave del mensaje, dato del error que lleva el mensaje). Con `min_length`,
#: `max_length`, `max_digits`... el dato es `count` (elige el plural); con `gt`, `ge`... es `limit`.
_BY_TYPE: Final[Mapping[str, tuple[str, str | None]]] = {
    **dict.fromkeys(
        ("missing", "missing_argument", "missing_keyword_only_argument", "missing_positional_only_argument"),
        ("INPUT_REQUIRED", None),
    ),
    **dict.fromkeys(
        ("extra_forbidden", "unexpected_keyword_argument", "unexpected_positional_argument", "no_such_attribute"),
        ("INPUT_NOT_ALLOWED", None),
    ),
    "string_too_short": ("INPUT_TOO_SHORT", "min_length"),
    "string_too_long": ("INPUT_TOO_LONG", "max_length"),
    "too_short": ("INPUT_TOO_FEW", "min_length"),
    "too_long": ("INPUT_TOO_MANY", "max_length"),
    "greater_than": ("INPUT_GREATER_THAN", "gt"),
    "greater_than_equal": ("INPUT_AT_LEAST", "ge"),
    "less_than": ("INPUT_LESS_THAN", "lt"),
    "less_than_equal": ("INPUT_AT_MOST", "le"),
    "multiple_of": ("INPUT_MULTIPLE_OF", "multiple_of"),
    "finite_number": ("INPUT_FINITE", None),
    **dict.fromkeys(("int_type", "int_parsing", "int_parsing_size", "int_from_float"), ("INPUT_INTEGER", None)),
    **dict.fromkeys(
        ("float_type", "float_parsing", "decimal_type", "decimal_parsing", "complex_type", "complex_str_parsing"),
        ("INPUT_NUMBER", None),
    ),
    "decimal_max_digits": ("INPUT_MAX_DIGITS", "max_digits"),
    "decimal_max_places": ("INPUT_MAX_DECIMALS", "decimal_places"),
    "decimal_whole_digits": ("INPUT_WHOLE_DIGITS", "whole_digits"),
    **dict.fromkeys(("bool_type", "bool_parsing"), ("INPUT_BOOLEAN", None)),
    **dict.fromkeys(("string_type", "string_sub_type", "string_unicode", "string_not_ascii"), ("INPUT_TEXT", None)),
    "string_pattern_mismatch": ("INPUT_PATTERN", None),
    **dict.fromkeys(("enum", "literal_error", "union_tag_invalid", "union_tag_not_found"), ("INPUT_CHOICE", None)),
    **dict.fromkeys(
        ("date_type", "date_parsing", "date_from_datetime_parsing", "date_from_datetime_inexact"), ("INPUT_DATE", None)
    ),
    **dict.fromkeys(("date_past", "datetime_past"), ("INPUT_PAST", None)),
    **dict.fromkeys(("date_future", "datetime_future"), ("INPUT_FUTURE", None)),
    **dict.fromkeys(("time_type", "time_parsing"), ("INPUT_TIME", None)),
    **dict.fromkeys(
        ("datetime_type", "datetime_parsing", "datetime_object_invalid", "datetime_from_date_parsing"),
        ("INPUT_DATETIME", None),
    ),
    **dict.fromkeys(("timezone_naive", "timezone_aware", "timezone_offset"), ("INPUT_TIMEZONE", None)),
    **dict.fromkeys(("time_delta_type", "time_delta_parsing"), ("INPUT_DURATION", None)),
    **dict.fromkeys(("list_type", "tuple_type", "set_type", "frozen_set_type", "iterable_type"), ("INPUT_LIST", None)),
    **dict.fromkeys(
        (
            "dict_type",
            "mapping_type",
            "model_type",
            "model_attributes_type",
            "dataclass_type",
            "dataclass_exact_type",
            "arguments_type",
        ),
        ("INPUT_OBJECT", None),
    ),
    **dict.fromkeys(("json_invalid", "json_type"), ("INPUT_JSON", None)),
    **dict.fromkeys(
        ("url_type", "url_parsing", "url_syntax_violation", "url_too_long", "url_scheme"), ("INPUT_URL", None)
    ),
    **dict.fromkeys(("uuid_type", "uuid_parsing", "uuid_version"), ("INPUT_UUID", None)),
}
#: Los datos de Pydantic que son una cantidad (eligen el plural del mensaje); los demás son un límite (`{limit}`).
_COUNTS: Final = frozenset({"min_length", "max_length", "max_digits", "decimal_places", "whole_digits"})


def message_of(error: Mapping[str, Any]) -> str:
    """El mensaje de un error de Pydantic en el idioma de la petición."""
    context = error.get("ctx") or {}
    original = context.get("error")
    if isinstance(original, LocalizedValueError):
        return original.message
    key, datum = _BY_TYPE.get(str(error.get("type")), ("INPUT_INVALID", None))
    if datum is None:
        return t(key)
    if datum not in context:  # Pydantic siempre lo manda; sin él, el genérico (nunca un marcador sin llenar)
        return t("INPUT_INVALID")
    name = "count" if datum in _COUNTS else "limit"
    return t(key, {name: context[datum]})


def validation_items(errors: Sequence[Mapping[str, Any]]) -> list[ErrorItem]:
    """Cada error de Pydantic como `ErrorItem` (código, campo y mensaje traducido)."""
    items = []
    for error in errors:
        # El primer elemento dice DÓNDE venía el dato (body, query, path, header, cookie) y se omite; solo ese: un
        # campo que se llame igual (p. ej. `path` en un cuerpo JSON) conserva su nombre.
        location = [str(part) for part in error.get("loc", [])][1:]
        items.append(
            ErrorItem(
                code=str(error.get("type", "invalid")).upper(),
                message=message_of(error),
                field=".".join(location) or None,
            )
        )
    return items
