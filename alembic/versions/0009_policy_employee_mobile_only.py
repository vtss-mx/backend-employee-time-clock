"""verification_policy.employee_mobile_only: empleados solo desde teléfono celular

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01 12:05:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "verification_policy",
        sa.Column("employee_mobile_only", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("verification_policy", "employee_mobile_only")
