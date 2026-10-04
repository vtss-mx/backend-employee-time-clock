"""Asistente de reportes de cada empresa (preguntas en español → consultas de SU empresa → Excel)

- Esquema nuevo `reporting`.
- `reporting.assistant_queries`: cada pregunta, cómo se interpretó y si sirvió. Es la memoria del
  asistente: de aquí salen las sugerencias y lo que aprende (el mantenimiento depura lo viejo).
- `reporting.learned_phrases`: palabras que cada empresa usa para sus datos, aprendidas de sus
  aclaraciones (nunca se comparten entre empresas).
- `reporting.saved_reports`: reportes guardados para volver a generarlos con datos al día.
- Pantalla `COMPANY_REPORTS` (menú de COMPANY, después de Validadores) y su permiso.

No modifica datos existentes.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-04 04:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
SCREEN = "COMPANY_REPORTS"
SCREEN_ROW = {
    "code": SCREEN,
    "name": "Reportes",
    "description": "Asistente de reportes: preguntas sobre los datos de la empresa y exportación a Excel.",
    "sort_order": 12,
    "active": True,
    "path": "/company/reports",
    "short_name": None,
    "icon": "FileSpreadsheet",
    "badge": None,
}
SCREEN_ORDER = 9


def _company_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["company_id"], ["tenancy.companies.id"], name=op.f(f"fk_{table}_company_id_companies"), ondelete="CASCADE"
    )


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["auth.users.id"], name=op.f(f"fk_{table}_{column}_users"), ondelete="SET NULL"
    )


def _queries() -> None:
    op.create_table(
        "assistant_queries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
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
        schema="reporting",
    )
    op.create_index(
        "ix_assistant_queries_company_created", "assistant_queries", ["company_id", "created_at"], schema="reporting"
    )
    op.create_index("ix_assistant_queries_created", "assistant_queries", ["created_at"], schema="reporting")


def _phrases() -> None:
    op.create_table(
        "learned_phrases",
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("phrase", sa.String(length=60), nullable=False),
        sa.Column("dataset", sa.String(length=40), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _company_fk("learned_phrases"),
        sa.PrimaryKeyConstraint("company_id", "phrase", "dataset", name=op.f("pk_learned_phrases")),
        schema="reporting",
    )


def _saved() -> None:
    op.create_table(
        "saved_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
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
        schema="reporting",
    )
    op.create_index(
        "uq_saved_reports_company_name",
        "saved_reports",
        ["company_id", sa.text("lower(name)")],
        unique=True,
        schema="reporting",
    )


def _screen() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    for row in seed["screens"]:  # orden del menú del seed: Reportes va después de Validadores
        op.execute(sa.update(screens).where(screens.c.code == row["code"]).values(sort_order=row["sort_order"]))
    # La pantalla tal como se creó (literal: 0043 la retiró del seed y del menú).
    table = sa.table("screens", *(sa.column(key) for key in SCREEN_ROW), schema="catalog")
    op.execute(postgresql.insert(table).values(**SCREEN_ROW).on_conflict_do_nothing(index_elements=["code"]))
    grants = sa.table("role_screens", sa.column("role_code"), sa.column("screen_code"), schema="catalog")
    op.execute(
        postgresql.insert(grants)
        .values(role_code="COMPANY", screen_code=SCREEN)
        .on_conflict_do_nothing(index_elements=["role_code", "screen_code"])
    )


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS reporting")
    _queries()
    _phrases()
    _saved()
    _screen()


def downgrade() -> None:
    grants = sa.table("role_screens", sa.column("screen_code"), schema="catalog")
    op.execute(sa.delete(grants).where(grants.c.screen_code == SCREEN))
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    op.execute(sa.delete(screens).where(screens.c.code == SCREEN))
    op.execute(
        sa.update(screens).where(screens.c.sort_order > SCREEN_ORDER).values(sort_order=screens.c.sort_order - 1)
    )
    op.drop_table("saved_reports", schema="reporting")
    op.drop_table("learned_phrases", schema="reporting")
    op.drop_table("assistant_queries", schema="reporting")
    op.execute("DROP SCHEMA IF EXISTS reporting")
