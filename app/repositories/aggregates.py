from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session


def group_counts(db: Session, stmt: Select[int | None, int]) -> dict[int, int]:
    """{clave: conteo} de un GROUP BY (las claves nulas se omiten)."""
    return {int(key): int(count) for key, count in db.execute(stmt) if key is not None}


def paginate[RowT](
    db: Session, stmt: Select[RowT], order: tuple[Any, ...], *, offset: int, limit: int
) -> tuple[list[RowT], int]:
    """Una página de `stmt` en el orden indicado y el total de filas sin paginar.

    Todos los listados paginados pasan por aquí: el conteo se hace sobre la misma consulta (con sus
    filtros) y `unique()` evita filas repetidas cuando el modelo carga relaciones con JOIN.
    """
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(*order).offset(offset).limit(limit)).unique().all()
    return list(items), int(total)
