"""Particiones mensuales (app/core/partitions.py): la única sentencia que las crea y las borra.

La API no tiene permisos de DDL: llama a `ops.ensure_partitions`, una función SECURITY DEFINER del dueño de la
base que solo acepta las tablas particionadas (`alembic/sql/0055_partitions.sql`) y que solo puede ejecutar el rol
de la plataforma (`app/core/db_roles.py`)."""

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session


def ensure(db: Session, table: str, today: date, months_ahead: int, keep_from: date | None) -> tuple[int, int]:
    """Crea los meses que faltan (del actual a `months_ahead` por delante) y borra los que terminan antes de
    `keep_from`; devuelve (creadas, borradas)."""
    row = db.execute(
        text("SELECT created, dropped FROM ops.ensure_partitions(:table, :today, :ahead, :keep)"),
        {"table": table, "today": today, "ahead": months_ahead, "keep": keep_from},
    ).one()
    return int(row.created), int(row.dropped)
