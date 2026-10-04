"""Errores del sistema en la base de datos y su seguimiento por el ADMIN

Cualquier error del backend (respuestas 4xx/5xx, excepciones no controladas, fallas en segundo
plano o en el canal en vivo) se registra agrupado para que el ADMIN de la plataforma lo vea y lo
marque: pendiente, en proceso, en revisión o solucionado (app/services/error_reporter.py).

- Esquema nuevo `ops` (operación de la plataforma).
- `ops.error_reports`: un error por huella (`fingerprint`) con su contador, primera y última vez,
  último traceId, detalle técnico y seguimiento (quién y cuándo). Uno solucionado que vuelve a
  ocurrir se reabre solo (`reopened`).
- `ops.error_occurrences`: las ocurrencias recientes (el mantenimiento depura las viejas).
- Catálogos `error_statuses` y `error_severities`, y la pantalla `ADMIN_ERRORS` con su contador.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-04 02:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
SCREEN = "ADMIN_ERRORS"
CATALOGS = ("error_statuses", "error_severities")


def _catalogs(seed: dict) -> None:
    for name in CATALOGS:
        table = op.create_table(
            name,
            sa.Column("tone", sa.String(length=20), server_default="muted", nullable=False),
            sa.Column("code", sa.String(length=30), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("description", sa.String(length=300), nullable=True),
            sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.PrimaryKeyConstraint("code", name=op.f(f"pk_{name}")),
            schema="catalog",
        )
        op.bulk_insert(table, seed[name])


def _reports() -> None:
    op.create_table(
        "error_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("severity", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="PENDING", nullable=False),
        sa.Column("code", sa.String(length=120), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("method", sa.String(length=10), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("exception_type", sa.String(length=255), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("occurrences", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("reopened", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_trace_id", sa.String(length=64), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_changed_by_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("occurrences > 0", name=op.f("ck_error_reports_occurrences_positive")),
        sa.CheckConstraint("source IN ('HTTP', 'LOG', 'WEBSOCKET')", name=op.f("ck_error_reports_source")),
        sa.ForeignKeyConstraint(
            ["severity"], ["catalog.error_severities.code"], name=op.f("fk_error_reports_severity_error_severities")
        ),
        sa.ForeignKeyConstraint(
            ["status"], ["catalog.error_statuses.code"], name=op.f("fk_error_reports_status_error_statuses")
        ),
        sa.ForeignKeyConstraint(
            ["status_changed_by_id"],
            ["auth.users.id"],
            name=op.f("fk_error_reports_status_changed_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_error_reports")),
        schema="ops",
    )
    op.create_index("uq_error_reports_fingerprint", "error_reports", ["fingerprint"], unique=True, schema="ops")
    op.create_index("ix_error_reports_status_seen", "error_reports", ["status", "last_seen_at", "id"], schema="ops")
    op.create_index("ix_error_reports_seen", "error_reports", ["last_seen_at", "id"], schema="ops")
    op.create_index("ix_error_reports_severity", "error_reports", ["severity"], schema="ops")
    op.create_index(
        "ix_error_reports_status_changed_by_id",
        "error_reports",
        ["status_changed_by_id"],
        schema="ops",
        postgresql_where=sa.text("status_changed_by_id IS NOT NULL"),
    )


def _occurrences() -> None:
    op.create_table(
        "error_occurrences",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["ops.error_reports.id"],
            name=op.f("fk_error_occurrences_report_id_error_reports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_error_occurrences")),
        schema="ops",
    )
    op.create_index("ix_error_occurrences_report", "error_occurrences", ["report_id", "id"], schema="ops")
    op.create_index("ix_error_occurrences_occurred_at", "error_occurrences", ["occurred_at"], schema="ops")


def _screen(seed: dict) -> None:
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    for row in seed["screens"]:  # orden del menú del seed: Errores va después de Empresas
        op.execute(sa.update(screens).where(screens.c.code == row["code"]).values(sort_order=row["sort_order"]))
    screen = next(r for r in seed["screens"] if r["code"] == SCREEN)
    table = sa.table("screens", *(sa.column(key) for key in screen), schema="catalog")
    op.execute(postgresql.insert(table).values(**screen).on_conflict_do_nothing(index_elements=["code"]))
    grants = sa.table("role_screens", sa.column("role_code"), sa.column("screen_code"), schema="catalog")
    for grant in (g for g in seed["role_screens"] if g["screen_code"] == SCREEN):
        op.execute(
            postgresql.insert(grants)
            .values(**grant)
            .on_conflict_do_nothing(index_elements=["role_code", "screen_code"])
        )


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    op.execute("CREATE SCHEMA IF NOT EXISTS ops")
    _catalogs(seed)
    _reports()
    _occurrences()
    _screen(seed)


def downgrade() -> None:
    grants = sa.table("role_screens", sa.column("screen_code"), schema="catalog")
    op.execute(sa.delete(grants).where(grants.c.screen_code == SCREEN))
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    op.execute(sa.delete(screens).where(screens.c.code == SCREEN))
    op.execute(sa.update(screens).where(screens.c.sort_order > 3).values(sort_order=screens.c.sort_order - 1))
    op.drop_table("error_occurrences", schema="ops")
    op.drop_table("error_reports", schema="ops")
    for name in reversed(CATALOGS):
        op.drop_table(name, schema="catalog")
    op.execute("DROP SCHEMA IF EXISTS ops")
