"""Seguridad por fila (Row-Level Security) en toda tabla de empresa: la última barrera del aislamiento

Decisión del dueño del producto (regla 14 del `AGENTS.md` raíz): por ninguna razón se mezclan los datos de una
empresa con los de otra. Con `0054` cada tabla de empresa tiene `company_id NOT NULL` y llaves compuestas; aquí la
base además FILTRA: cada una tiene RLS habilitada y FORZADA (también para su dueño) con la política
`tenant_isolation`:

    company_id = NULLIF(current_setting('app.company_id', true), '')::integer   -- para leer y para escribir

La API declara la empresa al empezar cada transacción (`set_config('app.company_id', N, true)`, solo de esa
transacción: correcto tras PgBouncer en modo transacción) a partir de la sesión o de la llave de integración; una
transacción sin empresa no ve ni escribe nada de estas tablas. El código de la plataforma (ADMIN, autenticación,
mantenimiento) cambia, solo dentro de su transacción, al rol sin login con `BYPASSRLS` (`DB_PLATFORM_ROLE`). Todo
en `app/core/row_security.py`; los roles los crea `python -m app.cli db roles` (lo corre `migrate` después de las
migraciones). Medido: la política se reduce a un filtro de una sola vez con el `company_id = :empresa` que ya
lleva cada consulta (README, "Seguridad por fila").

Una migración posterior que cambie DATOS de estas tablas corre con el dueño: en docker compose es superusuario y
la política no le aplica; en una base administrada el usuario de las migraciones necesita `BYPASSRLS`.

También los comentarios de esquemas y tablas (`alembic/sql/0056_comments.sql`) y el de `company_id` de cada
tabla de empresa.

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-05 23:00:00
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMMENTS_FILE = Path(__file__).resolve().parents[1] / "sql" / "0056_comments.sql"
EXPRESSION = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
COMPANY_COMMENT = (
    "Empresa dueña de la fila. Seguridad por fila (política tenant_isolation): solo se ve y se escribe en una "
    "transacción con app.company_id igual (o con el rol de la plataforma)."
)
TENANT_TABLES = (
    "workforce.employees",
    "workforce.departments",
    "workforce.department_managers",
    "workforce.validators",
    "workforce.validator_devices",
    "workforce.employee_qr_codes",
    "workforce.employee_status_events",
    "workforce.work_sites",
    "workforce.shifts",
    "workforce.shift_sites",
    "workforce.shift_assignments",
    "workforce.shift_change_requests",
    "workforce.company_holidays",
    "workforce.employee_absences",
    "workforce.employee_workdays",
    "biometrics.face_enrollments",
    "biometrics.face_enrollment_flags",
    "biometrics.face_embeddings",
    "biometrics.capture_fingerprints",
    "attendance.verification_logs",
    "attendance.work_sessions",
    "attendance.work_breaks",
    "attendance.attendance_events",
    "tenancy.verification_policy",
    "tenancy.company_api_keys",
    "tenancy.company_api_key_scopes",
    "ops.face_attempt_metrics",
    "ops.usage_daily",
    "ops.usage_routes",
    "ops.usage_users",
    "ops.storage_snapshots",
    "billing.plans",
    "billing.headcount_days",
    "billing.charges",
    "billing.charge_lines",
    "billing.payments",
    "billing.payment_allocations",
)
DOMAIN_SCHEMAS = ("auth", "tenancy", "workforce", "biometrics", "attendance", "catalog", "ops", "billing")


def upgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({EXPRESSION}) WITH CHECK ({EXPRESSION})")
        op.execute(f"COMMENT ON COLUMN {table}.company_id IS '{COMPANY_COMMENT}'")
    op.get_bind().exec_driver_sql(COMMENTS_FILE.read_text(encoding="utf-8").replace("%", "%%"))


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"COMMENT ON COLUMN {table}.company_id IS NULL")
    for schema in DOMAIN_SCHEMAS:
        op.execute(f"COMMENT ON SCHEMA {schema} IS NULL")
    op.get_bind().exec_driver_sql(
        "DO $$ DECLARE r record; BEGIN FOR r IN SELECT c.oid::regclass AS t FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname IN "
        "('auth', 'tenancy', 'workforce', 'biometrics', 'attendance', 'catalog', 'ops', 'billing') "
        "AND c.relkind IN ('r', 'p') LOOP EXECUTE format('COMMENT ON TABLE %%s IS NULL', r.t); END LOOP; END $$"
    )
