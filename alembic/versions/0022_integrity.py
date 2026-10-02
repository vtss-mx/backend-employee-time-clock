"""Depuración de integridad: relaciones compuestas, índices de FK y consultas frecuentes, CHECKs

Relaciones que hasta ahora solo garantizaba el código y pasa a garantizar la base (FK compuestas):

- workforce.validators (user_id, company_id) → auth.users (id, company_id): la cuenta del
  validador es de la misma empresa que el validador.
- biometrics.face_enrollments (employee_id, company_id) → workforce.employees (id, company_id):
  la empresa copiada en el registro (para la bandeja) es la de su empleado.
- biometrics.face_embeddings (enrollment_id, employee_id) → biometrics.face_enrollments
  (id, employee_id): cada embedding es del mismo empleado que su registro facial.

Para cada una, la tabla de destino recibe la llave única que la FK necesita.

Índices:
- FK sin índice que la cubra: verification_policy.updated_by_id (parcial, SET NULL al borrar un
  usuario), role_screens.screen_code y validator_mode_methods.method_code.
- users.role: conteos por rol de toda la plataforma.
- employees (company_id, id) parcial de activos y aprobados: la galería facial de la empresa.
- auth_sessions (user_id, expires_at) parcial de no revocadas: las sesiones vigentes de un usuario.

Las demás columnas que guardan un código de catálogo (method, reason, status, mode...) no llevan
índice a propósito: los códigos del catálogo nunca se borran ni cambian (se desactivan), así que
la FK solo se verifica al insertar, y un índice de baja cardinalidad en tablas grandes (bitácora)
solo costaría escrituras.

CHECKs: límite de empleados > 0 (o sin límite), muestras > 0, dimensión > 0, contadores >= 0.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-02 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (tabla, esquema, nombre, columnas)
UNIQUES = [
    ("users", "auth", "uq_users_id_company", ["id", "company_id"]),
    ("employees", "workforce", "uq_employees_id_company", ["id", "company_id"]),
    ("face_enrollments", "biometrics", "uq_face_enrollments_id_employee", ["id", "employee_id"]),
]
# (nombre, tabla, esquema, columnas, destino, esquema destino, columnas destino)
COMPOSITE_FKS = [
    ("fk_validators_user_company", "validators", "workforce", ["user_id", "company_id"], "users", "auth", ["id", "company_id"]),
    (
        "fk_face_enrollments_employee_company",
        "face_enrollments",
        "biometrics",
        ["employee_id", "company_id"],
        "employees",
        "workforce",
        ["id", "company_id"],
    ),
    (
        "fk_face_embeddings_enrollment_employee",
        "face_embeddings",
        "biometrics",
        ["enrollment_id", "employee_id"],
        "face_enrollments",
        "biometrics",
        ["id", "employee_id"],
    ),
]
# (nombre, tabla, esquema, columnas, condición del índice parcial)
INDEXES = [
    ("ix_users_role", "users", "auth", ["role"], None),
    ("ix_auth_sessions_user_open", "auth_sessions", "auth", ["user_id", "expires_at"], "revoked_at IS NULL"),
    ("ix_verification_policy_updated_by_id", "verification_policy", "tenancy", ["updated_by_id"], "updated_by_id IS NOT NULL"),
    (
        "ix_employees_company_approved",
        "employees",
        "workforce",
        ["company_id", "id"],
        "active IS TRUE AND face_status = 'APPROVED'",
    ),
    ("ix_role_screens_screen_code", "role_screens", "catalog", ["screen_code"], None),
    ("ix_validator_mode_methods_method_code", "validator_mode_methods", "catalog", ["method_code"], None),
]
# (tabla, esquema, nombre, condición)
CHECKS = [
    ("companies", "tenancy", "ck_companies_max_employees_positive", "max_employees IS NULL OR max_employees > 0"),
    ("face_enrollments", "biometrics", "ck_face_enrollments_samples_positive", "samples > 0"),
    ("face_embeddings", "biometrics", "ck_face_embeddings_dimension_positive", "dimension > 0"),
    ("rate_limit_counters", "auth", "ck_rate_limit_counters_count_not_negative", "count >= 0"),
]


def upgrade() -> None:
    for table, schema, name, columns in UNIQUES:
        op.create_unique_constraint(op.f(name), table, columns, schema=schema)
    for name, table, schema, columns, target, target_schema, target_columns in COMPOSITE_FKS:
        op.create_foreign_key(
            op.f(name), table, target, columns, target_columns, source_schema=schema, referent_schema=target_schema, ondelete="CASCADE"
        )
    for name, table, schema, columns, where in INDEXES:
        op.create_index(op.f(name), table, columns, schema=schema, postgresql_where=sa.text(where) if where else None)
    for table, schema, name, condition in CHECKS:
        op.create_check_constraint(op.f(name), table, condition, schema=schema)


def downgrade() -> None:
    for table, schema, name, _ in reversed(CHECKS):
        op.drop_constraint(op.f(name), table, schema=schema, type_="check")
    for name, table, schema, *_ in reversed(INDEXES):
        op.drop_index(op.f(name), table_name=table, schema=schema)
    for name, table, schema, *_ in reversed(COMPOSITE_FKS):
        op.drop_constraint(op.f(name), table, schema=schema, type_="foreignkey")
    for table, schema, name, _ in reversed(UNIQUES):
        op.drop_constraint(op.f(name), table, schema=schema, type_="unique")
