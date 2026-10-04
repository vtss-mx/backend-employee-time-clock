"""Índices de rendimiento: compuestos, parciales, únicos parciales, GIN de trigramas y BRIN

Cada índice responde a una consulta concreta de los repositorios (ver comentarios en los modelos).
Los índices de una sola columna que quedan cubiertos por el prefijo de uno compuesto se eliminan:
no aportan lecturas y sí cuestan en cada escritura.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-01 12:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Debe ser idéntica a app.models.employee.employee_search_text (la consulta usa la misma).
SEARCH_TEXT = "lower(first_name || ' ' || last_name || ' ' || employee_number || ' ' || coalesce(rfc, ''))"

# (tabla, índice simple que se reemplaza, índice nuevo, columnas)
REPLACED = [
    (
        "face_embeddings",
        "ix_face_embeddings_employee_id",
        "ix_face_embeddings_employee_active",
        ["employee_id", "active", "created_at"],
    ),
    (
        "face_enrollments",
        "ix_face_enrollments_employee_id",
        "ix_face_enrollments_employee_submitted",
        ["employee_id", "submitted_at", "id"],
    ),
    (
        "face_enrollments",
        "ix_face_enrollments_status",
        "ix_face_enrollments_status_submitted",
        ["status", "submitted_at", "id"],
    ),
    (
        "verification_logs",
        "ix_verification_logs_employee_id",
        "ix_verification_logs_employee_created",
        ["employee_id", "created_at", "id"],
    ),
    ("auth_sessions", "ix_auth_sessions_user_id", "ix_auth_sessions_user_created", ["user_id", "created_at"]),
]


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # Los valores únicos se comparan por igualdad exacta (sin lower/upper) para usar su índice;
    # se garantiza que todo lo existente ya esté normalizado.
    op.execute("UPDATE users SET email = lower(email) WHERE email <> lower(email)")
    op.execute(
        "UPDATE employees SET employee_number = upper(employee_number) WHERE employee_number <> upper(employee_number)"
    )

    for table, old, new, columns in REPLACED:
        op.create_index(new, table, columns)
        op.drop_index(old, table_name=table)

    op.create_index("ix_employees_name_order", "employees", ["last_name", "first_name", "id"])
    # Un solo QR activo por empleado, garantizado por la base de datos.
    op.create_index(
        "uq_employee_qr_codes_active_employee",
        "employee_qr_codes",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE"),
        sqlite_where=sa.text("active = 1"),
    )
    # FK con ON DELETE SET NULL: sin índice, borrar un usuario recorre la tabla completa.
    op.create_index(
        "ix_verification_logs_user_id",
        "verification_logs",
        ["user_id"],
        postgresql_where=sa.text("user_id IS NOT NULL"),
        sqlite_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "ix_face_enrollments_reviewed_by_id",
        "face_enrollments",
        ["reviewed_by_id"],
        postgresql_where=sa.text("reviewed_by_id IS NOT NULL"),
        sqlite_where=sa.text("reviewed_by_id IS NOT NULL"),
    )

    op.drop_index("ix_verification_logs_created_at", table_name="verification_logs")
    if _postgres():
        # Bitácora de solo inserción en orden de tiempo: BRIN en lugar de B-tree.
        op.create_index(
            "ix_verification_logs_created_at_brin", "verification_logs", ["created_at"], postgresql_using="brin"
        )
        # Búsqueda por fragmentos (LIKE '%texto%') con trigramas.
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(f"CREATE INDEX ix_employees_search_trgm ON employees USING gin (({SEARCH_TEXT}) gin_trgm_ops)")
        op.execute("CREATE INDEX ix_users_email_trgm ON users USING gin (email gin_trgm_ops)")
        # Estadísticas de las expresiones indexadas para que el planificador las use desde ya.
        op.execute("ANALYZE employees")
        op.execute("ANALYZE users")
    else:
        op.create_index("ix_verification_logs_created_at_brin", "verification_logs", ["created_at"])


def downgrade() -> None:
    if _postgres():
        op.execute("DROP INDEX IF EXISTS ix_users_email_trgm")
        op.execute("DROP INDEX IF EXISTS ix_employees_search_trgm")
    op.drop_index("ix_verification_logs_created_at_brin", table_name="verification_logs")
    op.create_index("ix_verification_logs_created_at", "verification_logs", ["created_at"])
    op.drop_index("ix_face_enrollments_reviewed_by_id", table_name="face_enrollments")
    op.drop_index("ix_verification_logs_user_id", table_name="verification_logs")
    op.drop_index("uq_employee_qr_codes_active_employee", table_name="employee_qr_codes")
    op.drop_index("ix_employees_name_order", table_name="employees")
    for table, old, new, columns in reversed(REPLACED):
        op.create_index(old, table, [columns[0]])
        op.drop_index(new, table_name=table)
