"""Teléfonos con lada internacional (E.164), solo tres roles y datos de usuario en la BD

- employees.phone y companies.phone pasan a E.164 (`+<lada><número>`); los números de 10 dígitos
  existentes son de México y se guardan como +52.
- users: CHECK de los tres roles (ADMIN, COMPANY, EMPLOYEE) y de su empresa (ADMIN sin empresa,
  COMPANY y EMPLOYEE siempre con una); preferencias de la interfaz en `preferences` (JSONB).
- remembered_accounts: "Recordar mi cuenta" vive en la BD (el dispositivo solo guarda una
  cookie HttpOnly opaca).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-01 23:30:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PHONE_TABLES = ("employees", "companies")


def upgrade() -> None:
    for table in PHONE_TABLES:
        op.alter_column(table, "phone", type_=sa.String(length=16), existing_nullable=True)
        op.execute(f"UPDATE {table} SET phone = '+52' || phone WHERE phone ~ '^[0-9]{{10}}$'")

    op.create_check_constraint(op.f("ck_users_role"), "users", "role IN ('ADMIN', 'COMPANY', 'EMPLOYEE')")
    op.create_check_constraint(op.f("ck_users_role_company"), "users", "(role = 'ADMIN') = (company_id IS NULL)")
    op.add_column(
        "users",
        sa.Column("preferences", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
    )

    op.create_table(
        "remembered_accounts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_remembered_accounts_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_remembered_accounts")),
    )
    op.create_index(op.f("ix_remembered_accounts_user_id"), "remembered_accounts", ["user_id"])
    op.create_index(op.f("ix_remembered_accounts_expires_at"), "remembered_accounts", ["expires_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_remembered_accounts_expires_at"), table_name="remembered_accounts")
    op.drop_index(op.f("ix_remembered_accounts_user_id"), table_name="remembered_accounts")
    op.drop_table("remembered_accounts")
    op.drop_column("users", "preferences")
    op.drop_constraint(op.f("ck_users_role_company"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_role"), "users", type_="check")
    for table in PHONE_TABLES:
        op.execute(f"UPDATE {table} SET phone = substr(phone, 4) WHERE phone ~ '^\\+52[0-9]{{10}}$'")
        op.execute(f"UPDATE {table} SET phone = NULL WHERE length(phone) > 10")
        op.alter_column(table, "phone", type_=sa.String(length=10), existing_nullable=True)
