"""employees.rfc: RFC de persona física, único

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable solo por los empleados existentes; la API lo exige en cada alta nueva.
    op.add_column("employees", sa.Column("rfc", sa.String(length=13), nullable=True))
    op.create_index(op.f("ix_employees_rfc"), "employees", ["rfc"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_employees_rfc"), table_name="employees")
    op.drop_column("employees", "rfc")
