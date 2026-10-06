from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, select, tuple_
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.repositories.aggregates import affected_rows
from app.repositories.storage_repository import StorageRepository

#: Llave primaria de una tabla: una columna o varias (tablas de consumo por empresa y día).
type PurgeKey = InstrumentedAttribute[Any] | tuple[InstrumentedAttribute[Any], ...]


def delete_batch(
    db: Session,
    key: PurgeKey,
    condition: ColumnElement[bool],
    limit: int,
    *,
    objects: InstrumentedAttribute[Any] | None = None,
) -> int:
    """Borra a lo más `limit` filas que cumplen `condition` (por su llave primaria) y dice cuántas.

    En lotes acotados: una depuración atrasada (tras una caída o al bajar una retención) nunca
    arma un DELETE gigante que choque con `statement_timeout` ni retenga bloqueos mucho tiempo.

    La condición se repite en el DELETE: así PostgreSQL busca las filas a borrar por el índice de la
    condición (las vencidas) en lugar de recorrer toda la tabla para unirla con el lote, y una fila que
    otra transacción renovó mientras tanto (p. ej. una huella de captura reutilizada) se vuelve a
    evaluar al bloquearla y ya no se borra.

    Con `objects` (la columna del objeto del bucket de una tabla de `STORED_IMAGES`), el `DELETE ... RETURNING`
    devuelve los objetos de las filas que DE VERDAD se borraron y se encolan para salir del bucket en la misma
    transacción (una fila que alguien restauró mientras tanto no se borra y su archivo se queda).
    """
    if isinstance(key, tuple):  # llave compuesta: (a, b, c) IN (SELECT a, b, c ...)
        rows = select(*key).where(condition).limit(limit)
        return affected_rows(db, delete(key[0].class_).where(tuple_(*key).in_(rows), condition))
    ids = select(key).where(condition).limit(limit).scalar_subquery()
    stmt = delete(key.class_).where(key.in_(ids), condition)
    if objects is None:
        return affected_rows(db, stmt)
    names = list(db.scalars(stmt.returning(objects).execution_options(synchronize_session=False)))
    StorageRepository(db).enqueue_many(names, datetime.now(UTC))
    return len(names)
