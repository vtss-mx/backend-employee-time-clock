"""Sesiones: "Recordar mi cuenta" (cookie persistente o de sesión del navegador)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-01 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Las sesiones abiertas antes de este cambio conservan su cookie persistente.
    op.add_column("auth_sessions", sa.Column("persistent", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.alter_column("auth_sessions", "persistent", server_default=sa.false())


def downgrade() -> None:
    op.drop_column("auth_sessions", "persistent")
