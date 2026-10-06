"""El turno dice dónde y cuándo se checa; la asignación es solo empleado, turno y fechas

Decisión del dueño del producto: "al asignar a uno o varios colaboradores se debe elegir un TURNO,
porque el turno contiene toda la información del domicilio donde checan y todo lo relacionado con el
horario". Hasta 0047 el turno era solo el horario y cada asignación guardaba su propio lugar (días
remotos en `shift_assignments.remote_weekdays` y sitios en `shift_assignment_sites`): asignar pedía
elegir otra vez sitios y días remotos, y dos personas con el mismo turno podían checar en lugares
distintos sin que el turno lo dijera. Ahora el turno es la única fuente de DÓNDE y CUÁNDO:

- `workforce.shifts.remote_weekdays`: días del turno en que se puede checar remoto. CHECK
  `ck_shifts_remote_weekdays`: bits válidos y solo días que el turno trabaja.
- `workforce.shift_sites` (shift_id, site_id, company_id): sitios donde se checa en persona con el
  turno. FK compuestas por empresa (como el resto del módulo): borrar el turno borra sus sitios
  (CASCADE); un sitio que algún turno usa no se borra (RESTRICT). Índice por sitio
  (`ix_shift_sites_site`): la revisión de esa FK al borrar un sitio, "¿qué turnos usan este sitio?" y
  los empleados que hoy checan en cada sitio. Los sitios de un turno salen de la llave primaria.
- Fuera `workforce.shift_assignment_sites` y `workforce.shift_assignments.remote_weekdays`: una
  asignación es solo empleado + turno + `valid_from` (+ `valid_to`).

Datos: se copian antes de borrar. Los sitios de cada turno son la unión de los sitios de todas sus
asignaciones (nadie pierde un lugar donde podía checar) y sus días remotos, el OR de los de sus
asignaciones limitado a los días que el turno trabaja hoy (el CHECK nuevo lo exige; un turno pudo
cambiar de días después de asignarse). El `downgrade` deja la estructura anterior y le copia a cada
asignación los sitios y días remotos de su turno. Las jornadas registradas no cambian (guardan su
copia de lo programado y el sitio donde se checó) y no se tocan los datos de otras tablas.

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-05 06:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "workforce"
SHIFT_REMOTE_CHECK = "ck_shifts_remote_weekdays"
ASSIGNMENT_REMOTE_CHECK = "ck_shift_assignments_remote_weekdays"


def upgrade() -> None:
    op.add_column(
        "shifts",
        sa.Column("remote_weekdays", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "shift_sites",
        sa.Column("shift_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["shift_id", "company_id"],
            ["workforce.shifts.id", "workforce.shifts.company_id"],
            name="fk_shift_sites_shift_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "company_id"],
            ["workforce.work_sites.id", "workforce.work_sites.company_id"],
            name="fk_shift_sites_site_company",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("shift_id", "site_id", name=op.f("pk_shift_sites")),
        schema=SCHEMA,
    )
    op.create_index("ix_shift_sites_site", "shift_sites", ["site_id"], unique=False, schema=SCHEMA)

    # Lo que cada asignación decía de su lugar pasa a su turno (antes de borrarlo).
    op.execute(
        """
        INSERT INTO workforce.shift_sites (shift_id, site_id, company_id)
        SELECT DISTINCT a.shift_id, s.site_id, a.company_id
        FROM workforce.shift_assignment_sites s
        JOIN workforce.shift_assignments a ON a.id = s.assignment_id AND a.company_id = s.company_id
        """
    )
    op.execute(
        """
        UPDATE workforce.shifts t
        SET remote_weekdays = r.remote & t.weekdays
        FROM (
            SELECT shift_id, company_id, bit_or(remote_weekdays) AS remote
            FROM workforce.shift_assignments
            GROUP BY shift_id, company_id
        ) r
        WHERE t.id = r.shift_id AND t.company_id = r.company_id
        """
    )
    op.create_check_constraint(
        op.f(SHIFT_REMOTE_CHECK),
        "shifts",
        "remote_weekdays BETWEEN 0 AND 127 AND (remote_weekdays & weekdays) = remote_weekdays",
        schema=SCHEMA,
    )

    op.drop_index("ix_shift_assignment_sites_site", table_name="shift_assignment_sites", schema=SCHEMA)
    op.drop_table("shift_assignment_sites", schema=SCHEMA)
    op.drop_constraint(op.f(ASSIGNMENT_REMOTE_CHECK), "shift_assignments", schema=SCHEMA, type_="check")
    op.drop_column("shift_assignments", "remote_weekdays", schema=SCHEMA)


def downgrade() -> None:
    op.add_column(
        "shift_assignments",
        sa.Column("remote_weekdays", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f(ASSIGNMENT_REMOTE_CHECK), "shift_assignments", "remote_weekdays BETWEEN 0 AND 127", schema=SCHEMA
    )
    op.create_table(
        "shift_assignment_sites",
        sa.Column("assignment_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assignment_id", "company_id"],
            ["workforce.shift_assignments.id", "workforce.shift_assignments.company_id"],
            name="fk_shift_assignment_sites_assignment_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "company_id"],
            ["workforce.work_sites.id", "workforce.work_sites.company_id"],
            name="fk_shift_assignment_sites_site_company",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("assignment_id", "site_id", name=op.f("pk_shift_assignment_sites")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_shift_assignment_sites_site", "shift_assignment_sites", ["site_id"], unique=False, schema=SCHEMA
    )

    # Cada asignación recupera el lugar de su turno (antes de borrarlo del turno).
    op.execute(
        """
        UPDATE workforce.shift_assignments a
        SET remote_weekdays = t.remote_weekdays
        FROM workforce.shifts t
        WHERE t.id = a.shift_id AND t.company_id = a.company_id
        """
    )
    op.execute(
        """
        INSERT INTO workforce.shift_assignment_sites (assignment_id, site_id, company_id)
        SELECT a.id, s.site_id, a.company_id
        FROM workforce.shift_assignments a
        JOIN workforce.shift_sites s ON s.shift_id = a.shift_id AND s.company_id = a.company_id
        """
    )

    op.drop_constraint(op.f(SHIFT_REMOTE_CHECK), "shifts", schema=SCHEMA, type_="check")
    op.drop_column("shifts", "remote_weekdays", schema=SCHEMA)
    op.drop_index("ix_shift_sites_site", table_name="shift_sites", schema=SCHEMA)
    op.drop_table("shift_sites", schema=SCHEMA)
