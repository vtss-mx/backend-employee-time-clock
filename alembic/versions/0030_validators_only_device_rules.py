"""Solo los validadores tienen restricciones de dispositivo

Los empleados (como los administradores) usan la aplicación desde cualquier dispositivo: se quita
la política `employee_mobile_only` ("empleados solo desde teléfono celular"). Los validadores
conservan las suyas: solo tableta o teléfono (`validator_mobile_only`) y dispositivo autorizado
por la empresa (`validator_device_approval`).

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-03 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("verification_policy", "employee_mobile_only", schema="tenancy")


def downgrade() -> None:
    op.add_column(
        "verification_policy",
        sa.Column("employee_mobile_only", sa.Boolean(), server_default=sa.true(), nullable=False),
        schema="tenancy",
    )
