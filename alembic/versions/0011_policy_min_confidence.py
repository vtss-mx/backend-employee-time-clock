"""verification_policy.min_confidence: nivel de confianza exigido (90 % por defecto)

También convierte la bitácora de verificaciones faciales de similitud coseno a confianza
calibrada, para que todo el historial use la misma escala que ahora se registra.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-01 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Misma calibración que app/facial_recognition/calibration.py (modelo de fusión).
A, B = -13.3133, 38.0366


def upgrade() -> None:
    op.add_column(
        "verification_policy",
        sa.Column("min_confidence", sa.Float(), server_default=sa.text("0.90"), nullable=False),
    )
    op.execute(
        f"UPDATE verification_logs SET score = round(CAST(1 / (1 + exp(-({A} + {B} * score))) AS numeric), 4) "
        "WHERE method = 'FACE' AND score IS NOT NULL AND (reason IS NULL OR reason = 'NO_MATCH')"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE verification_logs SET score = round(CAST((ln(score / (1 - score)) - ({A})) / {B} AS numeric), 4) "
        "WHERE method = 'FACE' AND score > 0 AND score < 1 AND (reason IS NULL OR reason = 'NO_MATCH')"
    )
    op.drop_column("verification_policy", "min_confidence")
