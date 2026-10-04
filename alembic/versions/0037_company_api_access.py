"""El ADMIN decide qué empresas tienen el módulo de Integraciones (API)

`tenancy.companies.api_enabled`: sin acceso, la pantalla "Integraciones (API)" no aparece en el menú
de la empresa, no puede administrar llaves y las que ya tenga dejan de funcionar (no se borran:
vuelven a servir si el ADMIN le da acceso de nuevo).

Para no cortar ninguna integración que ya funciona, las empresas que ya tienen alguna llave conservan
el acceso; las demás empiezan sin él (el ADMIN lo da desde la ficha de la empresa).

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-04 08:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("api_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        schema="tenancy",
    )
    op.execute(
        "UPDATE tenancy.companies SET api_enabled = true "
        "WHERE id IN (SELECT DISTINCT company_id FROM tenancy.company_api_keys)"
    )


def downgrade() -> None:
    op.drop_column("companies", "api_enabled", schema="tenancy")
