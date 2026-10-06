"""Búsqueda por texto de TODOS los listados (regla 21 del `AGENTS.md` raíz: sin inyección SQL), en un solo lugar.

Lo que escribe la persona se busca LITERAL: `contains_text` arma `expresión LIKE '%' || :término || '%' ESCAPE '/'`
con el término como parámetro y sus comodines escapados (`autoescape` de SQLAlchemy: `%` → `/%`, `_` → `/_`, `/` →
`//`). Así un `%` o un `_` no son comodines (antes de esta regla ya se escapaban en cada repositorio; ahora no se puede
olvidar: `tests/test_sql_safety.py` falla con un `LIKE`, `contains`, `startswith`... fuera de este módulo) y la barra
invertida tampoco es especial (con `ESCAPE` explícito, PostgreSQL y SQLite dejan de tratarla como escape). La sentencia
es la misma que miden `perf/db/run.sh` y los índices de trigramas (`pg_trgm`): PostgreSQL resuelve el patrón al planear
(sin sentencias preparadas, `prepare_threshold=None`) y usa el índice GIN de la columna o de la expresión.
"""

from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.sql.elements import SQLCoreOperations


def search_term(search: str | None) -> str:
    """El término normalizado: espacios de más fuera y en minúsculas (las columnas se comparan con `lower()`); `""` =
    sin búsqueda."""
    return " ".join((search or "").split()).lower()


def contains_text(expression: SQLCoreOperations[Any], term: str) -> ColumnElement[bool]:
    """`expression` contiene `term` LITERALMENTE (ver el docstring del módulo): el único `LIKE` de la aplicación."""
    return expression.contains(term, autoescape=True)
