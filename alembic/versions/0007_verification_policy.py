"""verification_policy: requisitos de verificación configurables por COMPANY

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table = op.create_table(
        "verification_policy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("block_glasses", sa.Boolean(), nullable=False),
        sa.Column("block_headwear", sa.Boolean(), nullable=False),
        sa.Column("block_mask", sa.Boolean(), nullable=False),
        sa.Column("liveness_challenge", sa.Boolean(), nullable=False),
        sa.Column("anti_spoofing", sa.Boolean(), nullable=False),
        sa.Column("qr_enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_verification_policy_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_policy")),
    )
    op.bulk_insert(
        table,
        [
            {
                "id": 1,
                "block_glasses": True,
                "block_headwear": True,
                "block_mask": True,
                "liveness_challenge": True,
                "anti_spoofing": True,
                "qr_enabled": True,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("verification_policy")
