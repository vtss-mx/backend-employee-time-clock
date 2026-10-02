"""Base de datos organizada por esquemas de dominio

auth        users, auth_sessions, remembered_accounts, rate_limit_counters
tenancy     companies, verification_policy
workforce   employees, employee_qr_codes
biometrics  face_enrollments, face_embeddings, face_challenges
attendance  verification_logs

`ALTER TABLE … SET SCHEMA` mueve también índices, restricciones y secuencias de cada tabla; las
llaves foráneas siguen apuntando a la misma tabla (por OID). La base queda con un search_path que
incluye los esquemas, para consultas manuales (psql) sin calificar.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01 23:59:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES_BY_SCHEMA = {
    "auth": ("users", "auth_sessions", "remembered_accounts", "rate_limit_counters"),
    "tenancy": ("companies", "verification_policy"),
    "workforce": ("employees", "employee_qr_codes"),
    "biometrics": ("face_enrollments", "face_embeddings", "face_challenges"),
    "attendance": ("verification_logs",),
}
SEARCH_PATH = ", ".join([*TABLES_BY_SCHEMA, "public"])


def _set_database_search_path(value: str) -> None:
    op.execute(
        f"DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET search_path TO {value}', current_database()); END $$"
    )


def upgrade() -> None:
    for schema, tables in TABLES_BY_SCHEMA.items():
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for table in tables:
            op.execute(f"ALTER TABLE public.{table} SET SCHEMA {schema}")
    _set_database_search_path(SEARCH_PATH)


def downgrade() -> None:
    for schema, tables in TABLES_BY_SCHEMA.items():
        for table in tables:
            op.execute(f"ALTER TABLE {schema}.{table} SET SCHEMA public")
        op.execute(f"DROP SCHEMA IF EXISTS {schema}")
    _set_database_search_path('"$user", public')
