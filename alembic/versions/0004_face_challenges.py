"""liveness challenges stored in the database (multi-process)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01 06:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "face_challenges",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_face_challenges_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_face_challenges")),
    )
    op.create_index(op.f("ix_face_challenges_user_id"), "face_challenges", ["user_id"])
    op.create_index(op.f("ix_face_challenges_expires_at"), "face_challenges", ["expires_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_face_challenges_expires_at"), table_name="face_challenges")
    op.drop_index(op.f("ix_face_challenges_user_id"), table_name="face_challenges")
    op.drop_table("face_challenges")
