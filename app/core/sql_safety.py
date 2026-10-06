"""SQL sin inyección (regla 21 del `AGENTS.md` raíz): lo único que el código escribe DENTRO del texto de una sentencia.

Los VALORES nunca van en el texto: viajan como parámetros (las expresiones del ORM o `:nombre` en `text()`), así la
base jamás los interpreta como SQL, vengan de donde vengan. En el texto solo quedan, además de las palabras fijas:

- **Identificadores** (esquema, tabla, columna, rol, base): solo de listas cerradas (constantes del código, `Literal`,
  los metadatos de los modelos o la configuración ya validada) y siempre con `sql_identifier`, que los revisa contra el
  juego de caracteres de un nombre sin comillas de PostgreSQL y los entrecomilla con el `IdentifierPreparer` de
  SQLAlchemy si son palabra reservada. Un nombre con cualquier otro carácter es un error del código, nunca SQL.
- **Literales en sentencias de utilidad** que PostgreSQL no deja parametrizar (`ALTER ROLE ... PASSWORD`, `COMMENT ON
  ...`): con `sql_literal`, que escapa las comillas con el procesador de literales de SQLAlchemy y rechaza lo que
  podría cambiar su sentido (NUL, barra invertida). Solo para textos que arma el propio código (el verificador SCRAM,
  un comentario fijo), nunca para algo que escribió una persona.

`tests/test_sql_safety.py` lee todo `app/` y `alembic/` y falla si una f-string, `%`, `.format`, `.join` o una
concatenación arman SQL con algo que no pase por aquí (o por la lista documentada de excepciones).
"""

import re
from typing import Final

from sqlalchemy import String
from sqlalchemy.dialects import postgresql

#: Una parte de un nombre sin comillas: minúsculas, números y `_`, hasta 63 (el límite de PostgreSQL).
_PART: Final = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_DIALECT: Final = postgresql.dialect()
_PREPARER: Final = _DIALECT.identifier_preparer
#: Literal de cadena de SQLAlchemy para PostgreSQL (duplica las comillas; `standard_conforming_strings`).
_QUOTE_LITERAL: Final = String().literal_processor(_DIALECT)
#: Lo que una literal escrita en el texto no puede llevar: NUL (PostgreSQL lo rechaza) y la barra invertida (con
#: `standard_conforming_strings = off` escaparía la comilla de cierre).
_UNSAFE_LITERAL: Final = re.compile(r"[\x00\\]")


def _identifier(name: str) -> str:
    parts = name.split(".")
    if len(parts) > 2 or not all(_PART.fullmatch(part) for part in parts):
        raise ValueError(f"Identificador SQL inválido: {name!r} (solo minúsculas, números y _, hasta 63)")
    return ".".join(_PREPARER.quote(part) for part in parts)


def sql_identifier(*names: str) -> str:
    """El nombre (o `esquema.nombre`) listo para escribirse en la SQL; varios, separados por coma (los roles de un
    `GRANT`). `ValueError` si alguno no es un nombre de una lista cerrada (minúsculas, números y `_`). Entrecomillado
    solo si es palabra reservada (`user` → `"user"`)."""
    return ", ".join(_identifier(name) for name in names)


def sql_literal(value: str) -> str:
    """La cadena como literal de SQL (`'...'`, comillas duplicadas) para una sentencia de utilidad sin parámetros.
    `ValueError` con NUL o barra invertida: solo textos del propio código, nunca lo que escribió una persona."""
    if _UNSAFE_LITERAL.search(value):
        raise ValueError("Literal SQL inválida: NUL o barra invertida")
    return str(_QUOTE_LITERAL(value))
