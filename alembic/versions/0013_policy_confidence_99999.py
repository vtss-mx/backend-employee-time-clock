"""verification_policy.min_confidence: 99.999 % (nivel solicitado por la empresa)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-01 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("verification_policy", "min_confidence", server_default=sa.text("0.99999"))
    op.execute("UPDATE verification_policy SET min_confidence = 0.99999")


def downgrade() -> None:
    op.execute("UPDATE verification_policy SET min_confidence = 0.90")
    op.alter_column("verification_policy", "min_confidence", server_default=sa.text("0.90"))
