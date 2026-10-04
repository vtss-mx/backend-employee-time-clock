"""face self-enrollment with company review

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01 05:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "face_enrollments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "APPROVED", "REJECTED", name="enrollmentstatus", native_enum=False, length=20),
            nullable=False,
        ),
        sa.Column("photo_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("photo_content_type", sa.String(length=30), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("liveness_passed", sa.Boolean(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name=op.f("fk_face_enrollments_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"], ["users.id"], name=op.f("fk_face_enrollments_reviewed_by_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_face_enrollments")),
    )
    with op.batch_alter_table("face_enrollments", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_face_enrollments_employee_id"), ["employee_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_face_enrollments_status"), ["status"], unique=False)

    with op.batch_alter_table("employees", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "face_status",
                sa.Enum(
                    "NOT_ENROLLED",
                    "PENDING_REVIEW",
                    "APPROVED",
                    "REJECTED",
                    name="facestatus",
                    native_enum=False,
                    length=20,
                ),
                server_default="NOT_ENROLLED",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("face_rejection_reason", sa.String(length=500), nullable=True))

    with op.batch_alter_table("face_embeddings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("enrollment_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_face_embeddings_enrollment_id"), ["enrollment_id"], unique=False)
        batch_op.create_foreign_key(
            batch_op.f("fk_face_embeddings_enrollment_id_face_enrollments"),
            "face_enrollments",
            ["enrollment_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # Empleados registrados con el flujo anterior (rostro capturado por COMPANY) ya están validados.
    op.execute(
        "UPDATE employees SET face_status = 'APPROVED' "
        "WHERE id IN (SELECT DISTINCT employee_id FROM face_embeddings WHERE active)"
    )


def downgrade() -> None:
    with op.batch_alter_table("face_embeddings", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_face_embeddings_enrollment_id_face_enrollments"), type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_face_embeddings_enrollment_id"))
        batch_op.drop_column("enrollment_id")
    with op.batch_alter_table("employees", schema=None) as batch_op:
        batch_op.drop_column("face_rejection_reason")
        batch_op.drop_column("face_status")
    op.drop_table("face_enrollments")
