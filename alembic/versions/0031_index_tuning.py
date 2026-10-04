"""Índices alineados con las consultas reales (auditoría de rendimiento)

Con `EXPLAIN` sobre las consultas de la aplicación:

- attendance.verification_logs:
  - (user_id, created_at, id) INCLUDE (success, method): historial del validador en su orden
    exacto, y los conteos de identificaciones del día y del bloqueo por fallos sin leer la tabla.
  - (company_id, id) en lugar de (company_id, created_at): bitácora de la empresa en orden de llegada,
    paginada y recorrida por cursor (API de integración).
  - Fuera el BRIN de created_at: ninguna consulta lo usaba (todas filtran por empresa, empleado o
    usuario) y solo costaba al insertar.
- workforce.validators: (company_id, lower(name), id), el orden real del listado.
- biometrics.face_enrollments: (company_id, submitted_at, id) para el historial sin filtro de estado.
- workforce.employee_qr_codes: (employee_id, id) para "último generado" y un parcial
  (employee_id, used_at, id) WHERE used_at IS NOT NULL para "último uso" (antes recorrían la tabla).
- Fuera índices redundantes o sin consulta: los de una sola columna que ya son prefijo de otro
  (company_api_keys.company_id, validator_devices.validator_id, employee_qr_codes.employee_id) y
  los de llaves foráneas a catálogos que nunca se borran (validator_devices.status,
  face_challenges.second_direction; esta última es además una tabla de mucho movimiento).

Solo cambia índices (ningún dato).

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-03 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (nombre, tabla, esquema) de los índices que se retiran; columnas para recrearlos al revertir.
DROPPED = (
    ("ix_verification_logs_created_at_brin", "verification_logs", "attendance", ["created_at"], "brin"),
    ("ix_company_api_keys_company_id", "company_api_keys", "tenancy", ["company_id"], None),
    ("ix_validator_devices_validator_id", "validator_devices", "workforce", ["validator_id"], None),
    ("ix_validator_devices_status", "validator_devices", "workforce", ["status"], None),
    ("ix_face_challenges_second_direction", "face_challenges", "biometrics", ["second_direction"], None),
    ("ix_employee_qr_codes_employee_id", "employee_qr_codes", "workforce", ["employee_id"], None),
)


def upgrade() -> None:
    op.drop_index("ix_verification_logs_user_created", table_name="verification_logs", schema="attendance")
    op.create_index(
        "ix_verification_logs_user_created",
        "verification_logs",
        ["user_id", "created_at", "id"],
        schema="attendance",
        postgresql_include=["success", "method"],
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.drop_index("ix_verification_logs_company_created", table_name="verification_logs", schema="attendance")
    op.create_index("ix_verification_logs_company_log", "verification_logs", ["company_id", "id"], schema="attendance")
    op.drop_index("ix_validators_company_name", table_name="validators", schema="workforce")
    op.create_index(
        "ix_validators_company_name", "validators", ["company_id", sa.text("lower(name)"), "id"], schema="workforce"
    )
    op.create_index(
        "ix_face_enrollments_company_submitted",
        "face_enrollments",
        ["company_id", "submitted_at", "id"],
        schema="biometrics",
    )
    op.create_index(
        "ix_employee_qr_codes_employee_latest", "employee_qr_codes", ["employee_id", "id"], schema="workforce"
    )
    op.create_index(
        "ix_employee_qr_codes_employee_used",
        "employee_qr_codes",
        ["employee_id", "used_at", "id"],
        schema="workforce",
        postgresql_where=sa.text("used_at IS NOT NULL"),
    )
    for name, table, schema, _, _ in DROPPED:
        op.drop_index(name, table_name=table, schema=schema)


def downgrade() -> None:
    for name, table, schema, columns, using in DROPPED:
        op.create_index(name, table, columns, schema=schema, **({"postgresql_using": using} if using else {}))
    op.drop_index("ix_employee_qr_codes_employee_used", table_name="employee_qr_codes", schema="workforce")
    op.drop_index("ix_employee_qr_codes_employee_latest", table_name="employee_qr_codes", schema="workforce")
    op.drop_index("ix_face_enrollments_company_submitted", table_name="face_enrollments", schema="biometrics")
    op.drop_index("ix_validators_company_name", table_name="validators", schema="workforce")
    op.create_index("ix_validators_company_name", "validators", ["company_id", "name"], schema="workforce")
    op.drop_index("ix_verification_logs_company_log", table_name="verification_logs", schema="attendance")
    op.create_index(
        "ix_verification_logs_company_created", "verification_logs", ["company_id", "created_at"], schema="attendance"
    )
    op.drop_index("ix_verification_logs_user_created", table_name="verification_logs", schema="attendance")
    op.create_index(
        "ix_verification_logs_user_created",
        "verification_logs",
        ["user_id", "created_at"],
        schema="attendance",
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
