"""Llaves de la API de integración por empresa

Cada empresa conecta sus sistemas (nómina, ERP...) a SU información con llaves propias:

- catalog.api_scopes: permisos de solo lectura que puede tener una llave (empleados,
  identificaciones, validadores).
- catalog.api_key_statuses: nombre y tono de cada estado (activa, vencida, revocada).
- tenancy.company_api_keys: la llave (solo el SHA-256 de su secreto y un prefijo para reconocerla),
  quién la creó o revocó, su vencimiento y su último uso.
- tenancy.company_api_key_scopes: los permisos de cada llave.
- Pantalla COMPANY_API ("Integraciones (API)") para el administrador de la empresa; las pantallas
  siguientes se recorren un lugar en el menú.

En una base nueva 0021 ya cargó la pantalla y su permiso desde el seed: aquí se insertan con
ON CONFLICT DO NOTHING.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-03 20:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
SCREEN = "COMPANY_API"


def _catalog_columns() -> list[sa.Column]:
    return [
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
    ]


def _catalogs(seed: dict) -> None:
    scopes = op.create_table(
        "api_scopes", *_catalog_columns(), sa.PrimaryKeyConstraint("code", name=op.f("pk_api_scopes")), schema="catalog"
    )
    op.bulk_insert(scopes, seed["api_scopes"])
    statuses = op.create_table(
        "api_key_statuses",
        sa.Column("tone", sa.String(length=20), server_default="muted", nullable=False),
        *_catalog_columns(),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_api_key_statuses")),
        schema="catalog",
    )
    op.bulk_insert(statuses, seed["api_key_statuses"])


def _keys() -> None:
    op.create_table(
        "company_api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_id", sa.Integer(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=45), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_company_api_keys_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["auth.users.id"],
            name=op.f("fk_company_api_keys_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_id"],
            ["auth.users.id"],
            name=op.f("fk_company_api_keys_revoked_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_api_keys")),
        schema="tenancy",
    )
    op.create_index(op.f("ix_company_api_keys_company_id"), "company_api_keys", ["company_id"], schema="tenancy")
    op.create_index(
        op.f("ix_company_api_keys_key_hash"), "company_api_keys", ["key_hash"], unique=True, schema="tenancy"
    )
    op.create_index(
        "ix_company_api_keys_company_created", "company_api_keys", ["company_id", "created_at"], schema="tenancy"
    )
    for column in ("created_by_id", "revoked_by_id"):
        op.create_index(
            f"ix_company_api_keys_{column}",
            "company_api_keys",
            [column],
            schema="tenancy",
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
        )

    op.create_table(
        "company_api_key_scopes",
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["tenancy.company_api_keys.id"],
            name=op.f("fk_company_api_key_scopes_api_key_id_company_api_keys"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scope"], ["catalog.api_scopes.code"], name=op.f("fk_company_api_key_scopes_scope_api_scopes")
        ),
        sa.PrimaryKeyConstraint("api_key_id", "scope", name=op.f("pk_company_api_key_scopes")),
        schema="tenancy",
    )
    op.create_index(
        op.f("ix_company_api_key_scopes_scope"), "company_api_key_scopes", ["scope"], schema="tenancy"
    )


def _screen(seed: dict) -> None:
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    for row in seed["screens"]:  # orden del menú: Integraciones va después de Configuración
        op.execute(sa.update(screens).where(screens.c.code == row["code"]).values(sort_order=row["sort_order"]))
    screen = next(r for r in seed["screens"] if r["code"] == SCREEN)
    table = sa.table("screens", *(sa.column(key) for key in screen), schema="catalog")
    op.execute(postgresql.insert(table).values(**screen).on_conflict_do_nothing(index_elements=["code"]))
    for grant in (g for g in seed["role_screens"] if g["screen_code"] == SCREEN):
        grants = sa.table("role_screens", sa.column("role_code"), sa.column("screen_code"), schema="catalog")
        op.execute(
            postgresql.insert(grants).values(**grant).on_conflict_do_nothing(index_elements=["role_code", "screen_code"])
        )


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    _catalogs(seed)
    _keys()
    _screen(seed)


def downgrade() -> None:
    grants = sa.table("role_screens", sa.column("screen_code"), schema="catalog")
    op.execute(sa.delete(grants).where(grants.c.screen_code == SCREEN))
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    op.execute(sa.delete(screens).where(screens.c.code == SCREEN))
    op.execute(sa.update(screens).where(screens.c.sort_order > 8).values(sort_order=screens.c.sort_order - 1))
    op.drop_table("company_api_key_scopes", schema="tenancy")
    op.drop_table("company_api_keys", schema="tenancy")
    op.drop_table("api_key_statuses", schema="catalog")
    op.drop_table("api_scopes", schema="catalog")
