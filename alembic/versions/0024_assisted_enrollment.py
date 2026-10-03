"""Registro facial asistido: la empresa captura el rostro del empleado en persona

biometrics.face_enrollments.captured_by_id → auth.users (ON DELETE SET NULL): el administrador de la
empresa que capturó el rostro con su cámara, con el empleado presente. Ese registro queda aprobado
al momento (la empresa vio a la persona); None = el empleado se registró solo y la empresa lo revisó.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-03 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("face_enrollments", sa.Column("captured_by_id", sa.Integer(), nullable=True), schema="biometrics")
    op.create_foreign_key(
        op.f("fk_face_enrollments_captured_by_id_users"),
        "face_enrollments",
        "users",
        ["captured_by_id"],
        ["id"],
        source_schema="biometrics",
        referent_schema="auth",
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_face_enrollments_captured_by_id"),
        "face_enrollments",
        ["captured_by_id"],
        schema="biometrics",
        postgresql_where=sa.text("captured_by_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_face_enrollments_captured_by_id"), table_name="face_enrollments", schema="biometrics")
    op.drop_constraint(
        op.f("fk_face_enrollments_captured_by_id_users"), "face_enrollments", schema="biometrics", type_="foreignkey"
    )
    op.drop_column("face_enrollments", "captured_by_id", schema="biometrics")
