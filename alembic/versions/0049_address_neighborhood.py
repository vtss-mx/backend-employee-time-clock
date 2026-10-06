"""Colonia y referencias en el domicilio de validadores y sitios de trabajo

El dueño del producto pidió que el domicilio de un sitio se capture con los campos de un domicilio
mexicano completo, en este orden: país, estado o provincia, municipio o alcaldía, ciudad o localidad,
colonia o barrio, código postal, calle o vialidad, número exterior, número interior y referencias.
Faltaban dos, que se agregan al domicilio compartido (`AddressMixin`: `workforce.validators` y
`workforce.work_sites`, las mismas columnas en ambas tablas):

- `neighborhood` (colonia o barrio, hasta 120): en México la colonia distingue dos calles con el mismo
  nombre en una ciudad y la usan el correo, la paquetería y quien llega por primera vez. El mapa la
  llena sola (Google la entrega como `sublocality_level_1` o `neighborhood`).
- `reference_notes` (referencias, hasta 300, opcional): entrecalles o puntos cercanos para llegar. No
  se llama `references` porque es palabra reservada de PostgreSQL.

Ambas se agregan nulas: los domicilios guardados antes no tienen colonia y siguen leyéndose igual; el
esquema la exige al guardar, así que la empresa la completa la próxima vez que edite el registro (no se
inventa un valor). Agregar una columna nula sin valor por omisión solo cambia el catálogo de la tabla
(no la reescribe ni la bloquea más que un instante). Sin índices: ninguna consulta filtra ni ordena por
ellas. El `downgrade` las quita.

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-05 08:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "workforce"
#: Tablas con domicilio (`AddressMixin`).
TABLES = ("validators", "work_sites")


def _columns() -> list[sa.Column]:
    # Una columna de SQLAlchemy pertenece a una sola tabla: cada tabla recibe las suyas.
    return [
        sa.Column("neighborhood", sa.String(length=120), nullable=True),
        sa.Column("reference_notes", sa.String(length=300), nullable=True),
    ]


def upgrade() -> None:
    for table in TABLES:
        for column in _columns():
            op.add_column(table, column, schema=SCHEMA)


def downgrade() -> None:
    for table in TABLES:
        for column in reversed(_columns()):
            op.drop_column(table, column.name, schema=SCHEMA)
