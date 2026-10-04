"""Reportes simples: elegir qué exportar y de qué fecha a qué fecha (sin asistente)

Decisión del dueño del producto: Reportes debe ser simple y solo para las empresas. Se quitan el
asistente que entendía preguntas, su aprendizaje, los reportes guardados y el asistente del ADMIN.

- Se eliminan `reporting.assistant_queries`, `reporting.learned_phrases`, `reporting.saved_reports` y
  el esquema `reporting` (borrado confirmado por el dueño: solo había 3 preguntas de prueba).
- Se retira la pantalla `ADMIN_REPORTS` y su permiso; el menú se renumera.

`downgrade` vuelve a crear la estructura vacía (los datos borrados no se recuperan).

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-04 16:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
SCHEMA = "reporting"
SCREEN = "ADMIN_REPORTS"
SCREEN_ORDER = 3
OLD_SCREEN = {
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


def _screens() -> sa.TableClause:
    return sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")


def upgrade() -> None:
    grants = sa.table("role_screens", sa.column("role_code"), sa.column("screen_code"), schema="catalog")
    op.execute(sa.delete(grants).where(grants.c.screen_code == SCREEN))
    screens = _screens()
    op.execute(sa.delete(screens).where(screens.c.code == SCREEN))
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    for row in seed["screens"]:  # el menú del seed, ya sin los reportes del ADMIN
        op.execute(sa.update(screens).where(screens.c.code == row["code"]).values(sort_order=row["sort_order"]))
    for table in ("saved_reports", "learned_phrases", "assistant_queries"):
        op.drop_table(table, schema=SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")


def _company_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["company_id"], ["tenancy.companies.id"], name=op.f(f"fk_{table}_company_id_companies"), ondelete="CASCADE"
    )


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["auth.users.id"], name=op.f(f"fk_{table}_{column}_users"), ondelete="SET NULL"
    )


def _tables_downgrade() -> None:
    """La estructura como quedó en 0038 (vacía)."""
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.create_table(
        "assistant_queries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("question", sa.String(length=500), nullable=False),
        sa.Column("normalized", sa.String(length=500), nullable=False),
        sa.Column("dataset", sa.String(length=40), nullable=True),
        sa.Column("plan", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("unknown_terms", sa.String(length=500), nullable=True),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("exported", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("helpful", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _company_fk("assistant_queries"),
        _user_fk("assistant_queries", "user_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_queries")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_assistant_queries_company_created", "assistant_queries", ["company_id", "created_at"], schema=SCHEMA
    )
    op.create_index("ix_assistant_queries_created", "assistant_queries", ["created_at"], schema=SCHEMA)
    op.create_table(
        "learned_phrases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("phrase", sa.String(length=60), nullable=False),
        sa.Column("dataset", sa.String(length=40), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _company_fk("learned_phrases"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learned_phrases")),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_learned_phrases_company",
        "learned_phrases",
        ["company_id", "phrase", "dataset"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("company_id IS NOT NULL"),
    )
    op.create_index(
        "uq_learned_phrases_platform",
        "learned_phrases",
        ["phrase", "dataset"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("company_id IS NULL"),
    )
    op.create_table(
        "saved_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("question", sa.String(length=500), nullable=True),
        sa.Column("plan", sa.Text(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runs", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _company_fk("saved_reports"),
        _user_fk("saved_reports", "created_by_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_reports")),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_saved_reports_company_name",
        "saved_reports",
        ["company_id", sa.text("lower(name)")],
        unique=True,
        schema=SCHEMA,
    )
    op.create_index(
        "uq_saved_reports_platform_name",
        "saved_reports",
        [sa.text("lower(name)")],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("company_id IS NULL"),
    )


def downgrade() -> None:
    _tables_downgrade()
    screens = _screens()
    op.execute(
        sa.update(screens).where(screens.c.sort_order >= SCREEN_ORDER).values(sort_order=screens.c.sort_order + 1)
    )
    table = sa.table("screens", *(sa.column(key) for key in OLD_SCREEN), schema="catalog")
    op.execute(postgresql.insert(table).values(**OLD_SCREEN).on_conflict_do_nothing(index_elements=["code"]))
    grants = sa.table("role_screens", sa.column("role_code"), sa.column("screen_code"), schema="catalog")
    op.execute(
        postgresql.insert(grants)
        .values(role_code="ADMIN", screen_code=SCREEN)
        .on_conflict_do_nothing(index_elements=["role_code", "screen_code"])
    )
