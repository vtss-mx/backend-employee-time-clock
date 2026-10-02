"""Pantallas y permisos por rol en la base de datos (catalog.screens, catalog.role_screens)

El menú de cada usuario y la autorización de los endpoints salen de aquí: el frontend ya no tiene
menús ni roles fijos, arma el menú y las rutas con las pantallas que le envía el backend.

- catalog.screens: código, nombre, ruta base, etiqueta corta, ícono, contador y orden.
- catalog.role_screens: qué pantallas tiene cada rol (y, con ellas, qué endpoints puede usar).

Los registros salen de alembic/seed/catalogs.json, la misma fuente que usan las pruebas.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-02 12:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG = "catalog"
SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"


def upgrade() -> None:
    screens = op.create_table(
        "screens",
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("path", sa.String(length=120), nullable=False),
        sa.Column("short_name", sa.String(length=30), nullable=True),
        sa.Column("icon", sa.String(length=40), nullable=False),
        sa.Column("badge", sa.String(length=30), nullable=True),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_screens")),
        sa.UniqueConstraint("path", name=op.f("uq_screens_path")),
        schema=CATALOG,
    )
    role_screens = op.create_table(
        "role_screens",
        sa.Column("role_code", sa.String(length=30), nullable=False),
        sa.Column("screen_code", sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_code"], [f"{CATALOG}.roles.code"], name=op.f("fk_role_screens_role_code_roles"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["screen_code"],
            [f"{CATALOG}.screens.code"],
            name=op.f("fk_role_screens_screen_code_screens"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_code", "screen_code", name=op.f("pk_role_screens")),
        schema=CATALOG,
    )
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    for table in (screens, role_screens):
        op.bulk_insert(
            table, [{key: value for key, value in row.items() if key in table.c} for row in seed[table.name]]
        )


def downgrade() -> None:
    op.drop_table("role_screens", schema=CATALOG)
    op.drop_table("screens", schema=CATALOG)
