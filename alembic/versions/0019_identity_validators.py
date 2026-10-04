"""Validadores de identidad (rol VALIDATOR): identifican empleados por rostro o QR

- auth.users: rol VALIDATOR; COMPANY y VALIDATOR pertenecen a UNA empresa.
- workforce.validators: nombre y modo de cada validador (QR, FACE, QR_OR_FACE, QR_AND_FACE).
- tenancy.verification_policy.validator_mobile_only: solo desde tableta o teléfono.
- attendance.verification_logs: índice (user_id, created_at) para el historial de cada validador.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-02 02:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES_WITH_VALIDATOR = "role IN ('ADMIN', 'COMPANY', 'EMPLOYEE', 'VALIDATOR')"
COMPANY_ROLES_WITH_VALIDATOR = "(role IN ('COMPANY', 'VALIDATOR')) = (company_id IS NOT NULL)"


def _replace_check(name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), "users", schema="auth", type_="check")
    op.create_check_constraint(op.f(name), "users", condition, schema="auth")


def upgrade() -> None:
    _replace_check("ck_users_role", ROLES_WITH_VALIDATOR)
    _replace_check("ck_users_role_company", COMPANY_ROLES_WITH_VALIDATOR)

    op.create_table(
        "validators",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["auth.users.id"], name=op.f("fk_validators_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_validators_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validators")),
        sa.UniqueConstraint("user_id", name=op.f("uq_validators_user_id")),
        schema="workforce",
    )
    op.create_index(op.f("ix_validators_company_name"), "validators", ["company_id", "name"], schema="workforce")

    op.add_column(
        "verification_policy",
        sa.Column("validator_mobile_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        schema="tenancy",
    )

    op.drop_index(op.f("ix_verification_logs_user_id"), table_name="verification_logs", schema="attendance")
    op.create_index(
        op.f("ix_verification_logs_user_created"),
        "verification_logs",
        ["user_id", "created_at"],
        schema="attendance",
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_verification_logs_user_created"), table_name="verification_logs", schema="attendance")
    op.create_index(
        op.f("ix_verification_logs_user_id"),
        "verification_logs",
        ["user_id"],
        schema="attendance",
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.drop_column("verification_policy", "validator_mobile_only", schema="tenancy")
    op.drop_index(op.f("ix_validators_company_name"), table_name="validators", schema="workforce")
    op.drop_table("validators", schema="workforce")
    op.execute("DELETE FROM auth.users WHERE role = 'VALIDATOR'")
    _replace_check("ck_users_role_company", "(role = 'COMPANY') = (company_id IS NOT NULL)")
    _replace_check("ck_users_role", "role IN ('ADMIN', 'COMPANY', 'EMPLOYEE')")
