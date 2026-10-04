"""Asistente de reportes de la plataforma (ADMIN): pantalla y memoria propia

El ADMIN pregunta por los datos de la plataforma (empresas, sus administradores, llaves de la API,
errores y demanda del servidor) con el mismo motor que usan las empresas. Su memoria (preguntas,
vocabulario aprendido y reportes guardados) vive en las mismas tablas con `company_id` nulo: así
nunca se mezcla con la de una empresa.

- `reporting.assistant_queries.company_id` y `reporting.saved_reports.company_id` admiten nulo.
  Nombre de reporte único también entre los de la plataforma (`uq_saved_reports_platform_name`).
- `reporting.learned_phrases`: la llave primaria era (empresa, palabra, reporte) y no admite una
  empresa nula; pasa a un `id` y la unicidad a dos índices parciales (por empresa / plataforma).
- Pantalla `ADMIN_REPORTS` (menú del ADMIN, después de Empresas) y su permiso.

No modifica datos existentes.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-04 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "reporting"
SCREEN = "ADMIN_REPORTS"
SCREEN_ORDER = 3
#: La pantalla tal como se creó (la 0040 la retira y el seed ya no la tiene).
SCREEN_ROW = {
    "code": SCREEN,
    "name": "Reportes",
    "description": "Asistente de reportes de la plataforma: empresas, administradores, llaves de la API, errores y "
    "demanda del servidor; exportación a Excel.",
    "sort_order": SCREEN_ORDER,
    "active": True,
    "path": "/admin/reports",
    "short_name": None,
    "icon": "FileSpreadsheet",
    "badge": None,
}
COMPANY = sa.text("company_id IS NOT NULL")
PLATFORM = sa.text("company_id IS NULL")


def _phrases_upgrade() -> None:
    op.drop_constraint("pk_learned_phrases", "learned_phrases", schema=SCHEMA, type_="primary")
    op.execute(f"ALTER TABLE {SCHEMA}.learned_phrases ADD COLUMN id SERIAL")
    op.create_primary_key("pk_learned_phrases", "learned_phrases", ["id"], schema=SCHEMA)
    op.alter_column("learned_phrases", "company_id", nullable=True, schema=SCHEMA)
    op.create_index(
        "uq_learned_phrases_company",
        "learned_phrases",
        ["company_id", "phrase", "dataset"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=COMPANY,
    )
    op.create_index(
        "uq_learned_phrases_platform",
        "learned_phrases",
        ["phrase", "dataset"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=PLATFORM,
    )


def _screen() -> None:
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    # Reportes va después de Empresas: lo que estaba de ahí en adelante baja un lugar.
    op.execute(
        sa.update(screens).where(screens.c.sort_order >= SCREEN_ORDER).values(sort_order=screens.c.sort_order + 1)
    )
    table = sa.table("screens", *(sa.column(key) for key in SCREEN_ROW), schema="catalog")
    op.execute(postgresql.insert(table).values(**SCREEN_ROW).on_conflict_do_nothing(index_elements=["code"]))
    grants = sa.table("role_screens", sa.column("role_code"), sa.column("screen_code"), schema="catalog")
    op.execute(
        postgresql.insert(grants)
        .values(role_code="ADMIN", screen_code=SCREEN)
        .on_conflict_do_nothing(index_elements=["role_code", "screen_code"])
    )


def upgrade() -> None:
    op.alter_column("assistant_queries", "company_id", nullable=True, schema=SCHEMA)
    op.alter_column("saved_reports", "company_id", nullable=True, schema=SCHEMA)
    op.create_index(
        "uq_saved_reports_platform_name",
        "saved_reports",
        [sa.text("lower(name)")],
        unique=True,
        schema=SCHEMA,
        postgresql_where=PLATFORM,
    )
    _phrases_upgrade()
    _screen()


def downgrade() -> None:
    grants = sa.table("role_screens", sa.column("screen_code"), schema="catalog")
    op.execute(sa.delete(grants).where(grants.c.screen_code == SCREEN))
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    op.execute(sa.delete(screens).where(screens.c.code == SCREEN))
    op.execute(
        sa.update(screens).where(screens.c.sort_order > SCREEN_ORDER).values(sort_order=screens.c.sort_order - 1)
    )
    # Sin la plataforma, su memoria no tiene a quién pertenecer.
    for table in ("assistant_queries", "saved_reports", "learned_phrases"):
        op.execute(f"DELETE FROM {SCHEMA}.{table} WHERE company_id IS NULL")
    op.drop_index("uq_learned_phrases_platform", table_name="learned_phrases", schema=SCHEMA)
    op.drop_index("uq_learned_phrases_company", table_name="learned_phrases", schema=SCHEMA)
    op.drop_constraint("pk_learned_phrases", "learned_phrases", schema=SCHEMA, type_="primary")
    op.drop_column("learned_phrases", "id", schema=SCHEMA)
    op.alter_column("learned_phrases", "company_id", nullable=False, schema=SCHEMA)
    op.create_primary_key("pk_learned_phrases", "learned_phrases", ["company_id", "phrase", "dataset"], schema=SCHEMA)
    op.drop_index("uq_saved_reports_platform_name", table_name="saved_reports", schema=SCHEMA)
    op.alter_column("saved_reports", "company_id", nullable=False, schema=SCHEMA)
    op.alter_column("assistant_queries", "company_id", nullable=False, schema=SCHEMA)
