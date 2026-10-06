from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Double, ForeignKey, ForeignKeyConstraint, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_schemas import CATALOG
from app.core.soft_delete import SoftDeleteMixin

#: Predicados de los índices de las tablas con borrado lógico (`app/core/soft_delete.py`, migración 0068).
LIVE = "deleted_at IS NULL"
DELETED = "deleted_at IS NOT NULL"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AddressMixin:
    """Domicilio con su punto en el mapa (WGS84): las mismas columnas que el esquema `Address`.

    Lo comparten los registros con lugar físico (validadores, sitios de trabajo): una sola definición
    para las columnas, la validación (`app/schemas/address.py`) y su lectura y escritura.

    `neighborhood` (colonia o barrio) y `reference_notes` (referencias para llegar) llegaron en la
    migración 0049: son nulas en los domicilios guardados antes; el esquema exige la colonia al guardar.
    Las referencias no se llaman `references` porque es palabra reservada de PostgreSQL."""

    street: Mapped[str | None] = mapped_column(String(150))
    exterior_number: Mapped[str | None] = mapped_column(String(20))
    interior_number: Mapped[str | None] = mapped_column(String(20))
    postal_code: Mapped[str | None] = mapped_column(String(10))
    country_code: Mapped[str | None] = mapped_column(
        String(2), ForeignKey(f"{CATALOG}.countries.code", ondelete="RESTRICT")
    )
    state: Mapped[str | None] = mapped_column(String(100))
    municipality: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(100))
    neighborhood: Mapped[str | None] = mapped_column(String(120))
    reference_notes: Mapped[str | None] = mapped_column(String(300))
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)


def company_fk(table: str, column: str, target: str, *, ondelete: str = "CASCADE") -> ForeignKeyConstraint:
    """Llave foránea COMPUESTA `(column, company_id)` → `target(id, company_id)` (regla 14 del AGENTS.md raíz).

    La fila y la que referencia son de la MISMA empresa: la base rechaza ligar filas de dos empresas aunque el
    código se equivoque. `target` es `esquema.tabla` y debe tener su único `(id, company_id)`. El nombre sigue la
    convención `fk_<tabla>_<columna sin _id>_company`."""
    return ForeignKeyConstraint(
        [column, "company_id"],
        [f"{target}.id", f"{target}.company_id"],
        name=f"fk_{table}_{column.removesuffix('_id')}_company",
        ondelete=ondelete,
    )


def live_unique(name: str, *columns: Any) -> Index:
    """Índice único entre los registros VIGENTES (parcial `deleted_at IS NULL`): uno en la papelera no bloquea que un
    registro nuevo use su dato (decisión del dueño del producto: los datos únicos se pueden volver a usar). Restaurar
    lo revisa de nuevo (409 `RESTORE_CONFLICT` si otro ya lo tomó)."""
    return Index(name, *columns, unique=True, postgresql_where=text(LIVE), sqlite_where=text(LIVE))


def trash_index(table: str, *columns: Any) -> Index:
    """`ix_<tabla>_deleted`: SOLO las filas eliminadas (parcial `deleted_at IS NOT NULL`), así que es pequeño y
    insertar o editar un registro vigente no lo toca. Sirve a la papelera de cada empresa (`company_id`, más reciente
    primero: se lee hacia atrás) y a la depuración de lo eliminado hace más de `SOFT_DELETE_RETENTION_DAYS` (recorre
    solo este índice, nunca la tabla)."""
    return Index(f"ix_{table}_deleted", *columns, postgresql_where=text(DELETED), sqlite_where=text(DELETED))


__all__ = [
    "DELETED",
    "LIVE",
    "AddressMixin",
    "SoftDeleteMixin",
    "TimestampMixin",
    "company_fk",
    "live_unique",
    "trash_index",
]
