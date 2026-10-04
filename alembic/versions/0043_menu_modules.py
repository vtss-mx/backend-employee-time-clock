"""Menú por módulos y submódulos

- `catalog.menu_modules`: los módulos del menú lateral (Plataforma, Personal, Asistencia, Datos...)
  con su nombre, ícono y orden.
- `catalog.menu_module_screens`: en qué módulo va cada pantalla (en uno solo). Va en una tabla aparte
  y no como columna de `screens` para que las migraciones anteriores, que insertan pantallas copiando
  sus filas del seed, sigan funcionando en una base nueva.
- Las pantallas quedan con el orden, el nombre y la etiqueta corta del seed: "Validaciones" pasa al
  módulo Personal y "Asistencia" (empresa) se llama "Tablero del día" dentro del módulo Asistencia.
- Se retira el módulo de Reportes (decisión del dueño del producto): la pantalla `COMPANY_REPORTS` y
  su permiso. Sus tablas ya no existían (0040); no había datos que borrar.

No modifica datos de las empresas.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-04 23:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
#: La pantalla de Reportes que se retira (para restaurarla al revertir).
REPORTS_ROW = {
    "code": "COMPANY_REPORTS",
    "name": "Reportes",
    "description": "Exportar a Excel lo que se necesite, de una fecha a otra.",
    "sort_order": 12,
    "active": True,
    "path": "/company/reports",
    "short_name": None,
    "icon": "FileSpreadsheet",
    "badge": None,
}


def upgrade() -> None:
    modules = op.create_table(
        "menu_modules",
        sa.Column("icon", sa.String(length=40), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_menu_modules")),
        schema="catalog",
    )
    links = op.create_table(
        "menu_module_screens",
        sa.Column("screen_code", sa.String(length=30), nullable=False),
        sa.Column("module_code", sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(
            ["module_code"],
            ["catalog.menu_modules.code"],
            name=op.f("fk_menu_module_screens_module_code_menu_modules"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["screen_code"],
            ["catalog.screens.code"],
            name=op.f("fk_menu_module_screens_screen_code_screens"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("screen_code", name=op.f("pk_menu_module_screens")),
        schema="catalog",
    )
    op.create_index(
        "ix_menu_module_screens_module_code", "menu_module_screens", ["module_code"], unique=False, schema="catalog"
    )
    grants = sa.table("role_screens", sa.column("screen_code"), schema="catalog")
    op.execute(sa.delete(grants).where(grants.c.screen_code == REPORTS_ROW["code"]))
    reports = sa.table("screens", sa.column("code"), schema="catalog")
    op.execute(sa.delete(reports).where(reports.c.code == REPORTS_ROW["code"]))
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    op.bulk_insert(modules, seed["menu_modules"])
    op.bulk_insert(links, seed["menu_module_screens"])
    screens = sa.table(
        "screens",
        sa.column("code"),
        sa.column("name"),
        sa.column("short_name"),
        sa.column("sort_order"),
        schema="catalog",
    )
    for row in seed["screens"]:  # orden, nombre y etiqueta corta del seed
        op.execute(
            sa.update(screens)
            .where(screens.c.code == row["code"])
            .values(name=row["name"], short_name=row["short_name"], sort_order=row["sort_order"])
        )


def downgrade() -> None:
    op.drop_index("ix_menu_module_screens_module_code", table_name="menu_module_screens", schema="catalog")
    op.drop_table("menu_module_screens", schema="catalog")
    op.drop_table("menu_modules", schema="catalog")
    screens = sa.table("screens", *(sa.column(key) for key in REPORTS_ROW), schema="catalog")
    op.execute(sa.insert(screens).values(**REPORTS_ROW))
    grants = sa.table("role_screens", sa.column("role_code"), sa.column("screen_code"), schema="catalog")
    op.execute(sa.insert(grants).values(role_code="COMPANY", screen_code=REPORTS_ROW["code"]))
