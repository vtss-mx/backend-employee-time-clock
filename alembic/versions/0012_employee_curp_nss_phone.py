"""employees: CURP y NSS (únicos) y teléfono; el índice de búsqueda incluye CURP y NSS

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-01 19:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Debe ser idéntica a app.models.employee.employee_search_text.
SEARCH_TEXT = (
    "lower(first_name || ' ' || last_name || ' ' || employee_number || ' ' || coalesce(rfc, '')"
    " || ' ' || coalesce(curp, '') || ' ' || coalesce(nss, ''))"
)
PREVIOUS_SEARCH_TEXT = "lower(first_name || ' ' || last_name || ' ' || employee_number || ' ' || coalesce(rfc, ''))"


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # Nullable solo por los empleados existentes; la API los exige en cada alta nueva.
    op.add_column("employees", sa.Column("curp", sa.String(length=18), nullable=True))
    op.add_column("employees", sa.Column("nss", sa.String(length=11), nullable=True))
    op.add_column("employees", sa.Column("phone", sa.String(length=10), nullable=True))
    op.create_index(op.f("ix_employees_curp"), "employees", ["curp"], unique=True)
    op.create_index(op.f("ix_employees_nss"), "employees", ["nss"], unique=True)
    if _postgres():
        op.execute("DROP INDEX IF EXISTS ix_employees_search_trgm")
        op.execute(f"CREATE INDEX ix_employees_search_trgm ON employees USING gin (({SEARCH_TEXT}) gin_trgm_ops)")
        op.execute("ANALYZE employees")


def downgrade() -> None:
    if _postgres():
        op.execute("DROP INDEX IF EXISTS ix_employees_search_trgm")
        op.execute(
            f"CREATE INDEX ix_employees_search_trgm ON employees USING gin (({PREVIOUS_SEARCH_TEXT}) gin_trgm_ops)"
        )
    op.drop_index(op.f("ix_employees_nss"), table_name="employees")
    op.drop_index(op.f("ix_employees_curp"), table_name="employees")
    op.drop_column("employees", "phone")
    op.drop_column("employees", "nss")
    op.drop_column("employees", "curp")
