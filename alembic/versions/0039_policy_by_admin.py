"""La política de verificación de identidad la configura el ADMIN de la plataforma

Antes cada empresa editaba su propia política en su pantalla "Configuración". Ahora el ADMIN es el
responsable de configurarla para cada empresa (desde la ficha de la empresa en su consola): la
empresa, sus empleados y sus validadores solo la leen.

- Se retira la pantalla `COMPANY_SETTINGS` y su permiso para COMPANY; el menú se renumera.
- La política de cada empresa (`tenancy.verification_policy`) no cambia: los valores vigentes se
  conservan tal cual.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-04 14:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
SCREEN = "COMPANY_SETTINGS"
SCREEN_ORDER = 11
#: Cómo era la pantalla (para volver atrás).
OLD_SCREEN = {
    "code": SCREEN,
    "name": "Configuración",
    "description": "Política de verificación de identidad de la empresa.",
    "sort_order": SCREEN_ORDER,
    "active": True,
    "path": "/company/settings",
    "short_name": "Ajustes",
    "icon": "Settings2",
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
    for row in seed["screens"]:  # el menú del seed, ya sin "Configuración"
        op.execute(sa.update(screens).where(screens.c.code == row["code"]).values(sort_order=row["sort_order"]))


def downgrade() -> None:
    screens = _screens()
    op.execute(
        sa.update(screens).where(screens.c.sort_order >= SCREEN_ORDER).values(sort_order=screens.c.sort_order + 1)
    )
    table = sa.table("screens", *(sa.column(key) for key in OLD_SCREEN), schema="catalog")
    op.execute(postgresql.insert(table).values(**OLD_SCREEN).on_conflict_do_nothing(index_elements=["code"]))
    grants = sa.table("role_screens", sa.column("role_code"), sa.column("screen_code"), schema="catalog")
    op.execute(
        postgresql.insert(grants)
        .values(role_code="COMPANY", screen_code=SCREEN)
        .on_conflict_do_nothing(index_elements=["role_code", "screen_code"])
    )
