from typing import Any, Literal, cast

from sqlalchemy import CursorResult, Delete, Select, Update, func, insert, select
from sqlalchemy.orm import Session, class_mapper
from sqlalchemy.orm.attributes import instance_state


def group_counts(db: Session, stmt: Select[int | None, int]) -> dict[int, int]:
    """{clave: conteo} de un GROUP BY (las claves nulas se omiten)."""
    return {int(key): int(count) for key, count in db.execute(stmt) if key is not None}


def insert_many[RecordT](db: Session, records: list[RecordT]) -> list[RecordT]:
    """Varios registros nuevos del mismo modelo en UNA sentencia (INSERT ... VALUES ..., ... RETURNING) y
    los guardados de vuelta (con su id y sus valores por omisión), en cualquier orden.

    El flush del ORM inserta uno por uno donde la base no garantiza el orden de RETURNING (SQLite): así
    una operación masiva (asignar un turno o vacaciones a cientos) cuesta lo mismo en cualquier motor.
    Solo se envían las columnas que se asignaron (las demás toman su valor por omisión de la base).
    """
    if not records:
        return []
    model = type(records[0])
    columns = {attribute.key for attribute in class_mapper(model).column_attrs}
    rows = [{key: value for key, value in instance_state(record).dict.items() if key in columns} for record in records]
    return list(db.scalars(insert(model).returning(model), rows))


def affected_rows(db: Session, stmt: Update | Delete, *, synchronize: Literal[False, "fetch"] = False) -> int:
    """Ejecuta un UPDATE/DELETE masivo y dice cuántas filas cambió. Por omisión sin sincronizar la
    sesión ORM (pueden ser miles de filas); "fetch" cuando quien llama sigue usando esos objetos."""
    result = cast(CursorResult[Any], db.execute(stmt.execution_options(synchronize_session=synchronize)))
    return result.rowcount or 0


#: Tope del conteo en tablas que crecen sin fin (bitácoras): contar más filas no cambia lo que muestra
#: el paginador ("10 000+") y cuesta leer toda la historia en cada página.
LOG_COUNT_CAP = 10_000


def paginate[RowT](
    db: Session,
    stmt: Select[RowT],
    order: tuple[Any, ...],
    *,
    offset: int,
    limit: int,
    count_cap: int | None = None,
) -> tuple[list[RowT], int]:
    """Una página de `stmt` en el orden indicado y el total de filas sin paginar.

    Todos los listados paginados pasan por aquí: el conteo se hace sobre la misma consulta (con sus
    filtros) y `unique()` evita filas repetidas cuando el modelo carga relaciones con JOIN. Con
    `count_cap` se cuenta a lo más esa cantidad (bitácoras: el total exacto de millones de filas no
    sirve para paginar y leerlas todas en cada página sí cuesta).
    """
    counted = stmt.limit(count_cap) if count_cap else stmt
    total = db.scalar(select(func.count()).select_from(counted.subquery())) or 0
    items = db.scalars(stmt.order_by(*order).offset(offset).limit(limit)).unique().all()
    return list(items), int(total)
