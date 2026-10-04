"""face_enrollments.flagged_accessories (accesorios detectados que el empleado indicó no usar)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01 08:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("face_enrollments", sa.Column("flagged_accessories", sa.String(length=60), nullable=True))


def downgrade() -> None:
    op.drop_column("face_enrollments", "flagged_accessories")
