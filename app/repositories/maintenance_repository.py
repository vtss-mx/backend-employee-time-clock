from typing import Any

from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.repositories.aggregates import affected_rows


def delete_batch(db: Session, key: InstrumentedAttribute[Any], condition: ColumnElement[bool], limit: int) -> int:
    """Borra a lo más `limit` filas que cumplen `condition` (por su llave primaria) y dice cuántas.

    En lotes acotados: una depuración atrasada (tras una caída o al bajar una retención) nunca
    arma un DELETE gigante que choque con `statement_timeout` ni retenga bloqueos mucho tiempo.
    """
    table = key.class_
    ids = select(key).where(condition).limit(limit).scalar_subquery()
    return affected_rows(db, delete(table).where(key.in_(ids)))
