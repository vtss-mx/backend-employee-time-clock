"""Número de empleado opcional (decisión del dueño del producto, 2026-10-06: «el número de empleado debe ser opcional
también»)

El número se vuelve opcional como el RFC, la CURP y el NSS (README, «Datos opcionales del empleado»): sin capturar es
NULL, nunca una cadena vacía; con valor conserva su formato y es único en la empresa entre los empleados vigentes.

- `workforce.employees.employee_number` acepta NULL (`DROP NOT NULL`: solo cambia el catálogo de la tabla, no la
  reescribe ni la recorre).
- `uq_employees_company_number` NO cambia: es el único parcial de lo vigente `(company_id, employee_number) WHERE
  deleted_at IS NULL` (migración 0068) y un índice único de PostgreSQL admite varios NULL, igual que los de RFC, CURP y
  NSS (que tampoco necesitaron migración). Reconstruirlo con `AND employee_number IS NOT NULL` solo lo haría un poco
  más chico a cambio de construir un único sobre una tabla grande durante el despliegue.
- `ix_employees_company_search_trgm` (la búsqueda de empleados por nombre, número, RFC, CURP o NSS) cambia su expresión
  a `coalesce(employee_number, '')`: un NULL concatenado vuelve NULL todo el texto y la persona sin número ya no se
  encontraría ni por su nombre. Es la misma expresión de `employee_search_text()` en el modelo. Se construye
  `CONCURRENTLY` con nombre temporal, se borra el anterior y se renombra (§3.1.13: la búsqueda nunca se queda sin
  índice ni frena lecturas o escrituras); cada paso es repetible tras una falla a la mitad (`IF EXISTS`).

Compatible con la versión anterior en marcha (despliegue gradual, §4): esa versión nunca escribe un NULL y, mientras
termina de salir, su búsqueda (la expresión anterior) se resuelve con el índice del listado de la empresa en lugar del
de trigramas: más lenta durante esos minutos, con el mismo resultado.

`downgrade` deja exactamente lo anterior: la versión anterior exige un número, así que cada empleado sin número recibe
`ID-<id>` (su id es único: no choca con otro salvo que la empresa haya usado ese mismo formato, y entonces el único
detiene el `downgrade` sin cambiar nada), la columna vuelve a `NOT NULL` y la búsqueda a su expresión anterior. Sin SQL
armada: todo son sentencias constantes (regla 21 de la raíz).

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-06 21:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: El índice de la búsqueda con la expresión nueva (`coalesce` del número) y con la anterior, cada uno con nombre
#: temporal; después se borra el vigente y el temporal toma su nombre.
SEARCH_NEW = (
    "CREATE INDEX CONCURRENTLY ix_employees_company_search_trgm_new ON workforce.employees USING gin "
    "(company_id, (lower(first_name || ' ' || last_name || ' ' || coalesce(employee_number, '') || ' ' || "
    "coalesce(rfc, '') || ' ' || coalesce(curp, '') || ' ' || coalesce(nss, ''))) gin_trgm_ops)"
)
SEARCH_PREVIOUS = (
    "CREATE INDEX CONCURRENTLY ix_employees_company_search_trgm_new ON workforce.employees USING gin "
    "(company_id, (lower(first_name || ' ' || last_name || ' ' || employee_number || ' ' || "
    "coalesce(rfc, '') || ' ' || coalesce(curp, '') || ' ' || coalesce(nss, ''))) gin_trgm_ops)"
)
DROP_TEMPORARY = "DROP INDEX CONCURRENTLY IF EXISTS workforce.ix_employees_company_search_trgm_new"
DROP_CURRENT = "DROP INDEX CONCURRENTLY IF EXISTS workforce.ix_employees_company_search_trgm"
RENAME = "ALTER INDEX workforce.ix_employees_company_search_trgm_new RENAME TO ix_employees_company_search_trgm"

#: Cada reconstrucción, en orden: el temporal (repetible), el índice nuevo, sin el vigente y con su nombre.
REBUILD_NEW = (DROP_TEMPORARY, SEARCH_NEW, DROP_CURRENT, RENAME)
REBUILD_PREVIOUS = (DROP_TEMPORARY, SEARCH_PREVIOUS, DROP_CURRENT, RENAME)

OPTIONAL = "ALTER TABLE workforce.employees ALTER COLUMN employee_number DROP NOT NULL"
#: `downgrade`: la versión anterior exige un número (formato `^[A-Z0-9][A-Z0-9_-]{0,29}$`: `ID-<id>` lo cumple).
FILL_MISSING = "UPDATE workforce.employees SET employee_number = 'ID-' || id WHERE employee_number IS NULL"
REQUIRED = "ALTER TABLE workforce.employees ALTER COLUMN employee_number SET NOT NULL"


def upgrade() -> None:
    op.execute(OPTIONAL)
    # CONCURRENTLY no puede ir dentro de una transacción: cada sentencia se confirma sola.
    with op.get_context().autocommit_block():
        for statement in REBUILD_NEW:
            op.execute(statement)


def downgrade() -> None:
    op.execute(FILL_MISSING)
    op.execute(REQUIRED)
    with op.get_context().autocommit_block():
        for statement in REBUILD_PREVIOUS:
            op.execute(statement)
