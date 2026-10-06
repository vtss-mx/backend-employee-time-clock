from typing import Any, Literal, cast

from sqlalchemy import CursorResult, Delete, Select, Update, func, insert, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, class_mapper
from sqlalchemy.orm.attributes import instance_state

from app.core.soft_delete import INCLUDE_DELETED, WITH_DELETED, with_deleted


def get_scoped[RecordT](
    db: Session,
    model: type[RecordT],
    record_id: int,
    company_id: int,
    *,
    lock: bool = False,
    include_deleted: bool = False,
) -> RecordT | None:
    """Una fila de una tabla de empresa por su id, solo si es de `company_id` (si no, None: la ruta responde 404).

    Sin candado sale del mapa de identidad o de una consulta por la llave primaria. Con candado (`FOR UPDATE OF`
    solo esa tabla: lo que se carga con JOIN no se bloquea) la empresa va en el WHERE: nunca se bloquea, ni un
    instante, una fila de otra empresa con un id que mandó el cliente. La relectura (`populate_existing`) trae lo
    que otra transacción confirmó mientras se esperaba el candado. En una tabla con borrado lógico solo encuentra lo
    vigente, salvo `include_deleted` (eliminar, restaurar y el detalle que muestra la marca «Eliminado»)."""
    entity = cast(Any, model)
    options = WITH_DELETED if include_deleted else {}
    if not lock:
        found: Any = db.get(model, record_id, execution_options=options)
        return cast(RecordT, found) if found is not None and found.company_id == company_id else None
    stmt = (
        select(entity)
        .where(entity.id == record_id, entity.company_id == company_id)
        .with_for_update(of=entity)
        .execution_options(populate_existing=True, **options)
    )
    return cast(RecordT | None, db.scalar(stmt))


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


def dialect_insert(db: Session, model: Any) -> Any:
    """INSERT del motor en uso (PostgreSQL en producción, SQLite en las pruebas): el que trae
    `on_conflict_do_nothing` / `on_conflict_do_update` para una sentencia atómica."""
    return (postgresql if db.get_bind().dialect.name == "postgresql" else sqlite).insert(model)


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
    count = select(func.count()).select_from(counted.subquery())
    if stmt.get_execution_options().get(INCLUDE_DELETED):  # la papelera también se cuenta con lo eliminado
        count = with_deleted(count)
    total = db.scalar(count) or 0
    items = db.scalars(stmt.order_by(*order).offset(offset).limit(limit)).unique().all()
    return list(items), int(total)


def trash_page[RowT](db: Session, stmt: Select[RowT], model: Any, *, offset: int, limit: int) -> tuple[list[RowT], int]:
    """Una página de la papelera («Eliminados») de `stmt` —ya acotada a su empresa y sus filtros—, lo eliminado más
    reciente primero. La lee el índice parcial `ix_<tabla>_deleted` (`company_id, deleted_at, id` hacia atrás): solo
    filas eliminadas, nunca las vigentes."""
    trash = with_deleted(stmt.where(model.deleted_at.is_not(None)))
    return paginate(db, trash, (model.deleted_at.desc(), model.id.desc()), offset=offset, limit=limit)
