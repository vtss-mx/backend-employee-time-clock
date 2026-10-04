"""Una persona puede trabajar en varias empresas; correo y teléfono únicos en la plataforma

- auth.users = la persona (cuenta): correo y teléfono únicos. El teléfono pasa de cada empleado a
  su cuenta.
- workforce.employees = un empleo por empresa: (company_id, user_id) único; ya no uno por usuario.
- auth.users.company_id queda solo para COMPANY (administrador de una empresa).
- auth.auth_sessions.company_id = empresa en la que entró el empleado (la elige al iniciar sesión
  si trabaja en varias). Las sesiones vigentes conservan la empresa de su único empleo.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-02 00:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Teléfono de la persona (único).
    op.add_column("users", sa.Column("phone", sa.String(length=16), nullable=True), schema="auth")
    op.execute(
        "UPDATE auth.users u SET phone = e.phone FROM workforce.employees e "
        "WHERE e.user_id = u.id AND e.phone IS NOT NULL"
    )
    op.create_unique_constraint(op.f("uq_users_phone"), "users", ["phone"], schema="auth")
    op.drop_column("employees", "phone", schema="workforce")

    # Un empleo por empresa (antes: uno por usuario).
    op.drop_constraint(op.f("uq_employees_user_id"), "employees", schema="workforce", type_="unique")
    op.create_index(
        op.f("uq_employees_company_user"), "employees", ["company_id", "user_id"], unique=True, schema="workforce"
    )
    op.create_index(op.f("ix_employees_user"), "employees", ["user_id"], schema="workforce")

    # La empresa de un empleado vive en sus empleos; la cuenta solo la tiene COMPANY.
    op.drop_constraint(op.f("ck_users_role_company"), "users", schema="auth", type_="check")
    op.execute("UPDATE auth.users SET company_id = NULL WHERE role = 'EMPLOYEE'")
    op.create_check_constraint(
        op.f("ck_users_role_company"), "users", "(role = 'COMPANY') = (company_id IS NOT NULL)", schema="auth"
    )

    # Empresa elegida en cada sesión.
    op.add_column("auth_sessions", sa.Column("company_id", sa.Integer(), nullable=True), schema="auth")
    op.create_foreign_key(
        op.f("fk_auth_sessions_company_id_companies"),
        "auth_sessions",
        "companies",
        ["company_id"],
        ["id"],
        source_schema="auth",
        referent_schema="tenancy",
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_auth_sessions_company_id"), "auth_sessions", ["company_id"], schema="auth")
    op.execute(
        "UPDATE auth.auth_sessions s SET company_id = e.company_id FROM workforce.employees e "
        "WHERE e.user_id = s.user_id"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_sessions_company_id"), table_name="auth_sessions", schema="auth")
    op.drop_constraint(
        op.f("fk_auth_sessions_company_id_companies"), "auth_sessions", schema="auth", type_="foreignkey"
    )
    op.drop_column("auth_sessions", "company_id", schema="auth")

    op.drop_constraint(op.f("ck_users_role_company"), "users", schema="auth", type_="check")
    op.execute(
        "UPDATE auth.users u SET company_id = e.company_id FROM workforce.employees e "
        "WHERE e.user_id = u.id AND u.role = 'EMPLOYEE'"
    )
    op.create_check_constraint(
        op.f("ck_users_role_company"), "users", "(role = 'ADMIN') = (company_id IS NULL)", schema="auth"
    )

    op.drop_index(op.f("ix_employees_user"), table_name="employees", schema="workforce")
    op.drop_index(op.f("uq_employees_company_user"), table_name="employees", schema="workforce")
    op.create_unique_constraint(op.f("uq_employees_user_id"), "employees", ["user_id"], schema="workforce")

    op.add_column("employees", sa.Column("phone", sa.String(length=16), nullable=True), schema="workforce")
    op.execute("UPDATE workforce.employees e SET phone = u.phone FROM auth.users u WHERE u.id = e.user_id")
    op.drop_constraint(op.f("uq_users_phone"), "users", schema="auth", type_="unique")
    op.drop_column("users", "phone", schema="auth")
