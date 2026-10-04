"""La base de datos se organiza por esquemas de dominio (PostgreSQL)."""

from sqlalchemy import inspect

import app.models  # noqa: F401  (registra todas las tablas)
from app.core.database import Base, engine
from app.core.db_schemas import ALL_SCHEMAS, ATTENDANCE, AUTH, BIOMETRICS, CATALOG, OPS, TENANCY, WORKFORCE
from app.models.catalog_seed import load_catalog_seed

EXPECTED = {
    AUTH: {"users", "auth_sessions", "remembered_accounts", "rate_limit_counters"},
    TENANCY: {"companies", "verification_policy", "company_api_keys", "company_api_key_scopes"},
    WORKFORCE: {
        "employees",
        "employee_qr_codes",
        "validators",
        "validator_devices",
        "departments",
        "department_managers",
        "work_sites",
        "shifts",
        "shift_assignments",
        "shift_assignment_sites",
        "shift_change_requests",
        "company_holidays",
        "employee_absences",
        "employee_workdays",
    },
    BIOMETRICS: {
        "face_enrollments",
        "face_enrollment_flags",
        "face_embeddings",
        "face_challenges",
        "capture_fingerprints",
    },
    ATTENDANCE: {"verification_logs", "work_sessions", "work_breaks", "attendance_events"},
    OPS: {"error_reports", "error_occurrences", "face_attempt_metrics", "security_thresholds"},
    # Cada tabla del catálogo tiene sus registros en alembic/seed/catalogs.json (y viceversa).
    CATALOG: set(load_catalog_seed()),
}


def test_every_table_belongs_to_a_domain_schema():
    by_schema: dict[str, set[str]] = {}
    for table in Base.metadata.tables.values():
        assert table.schema in ALL_SCHEMAS, f"{table.name} no tiene esquema de dominio"
        by_schema.setdefault(table.schema, set()).add(table.name)
    assert by_schema == EXPECTED


def test_tables_live_in_their_schemas_in_postgresql():
    if engine.dialect.name != "postgresql":
        return  # SQLite (pruebas rápidas) no tiene esquemas
    inspector = inspect(engine)
    for schema, tables in EXPECTED.items():
        assert tables <= set(inspector.get_table_names(schema=schema)), schema
    assert not EXPECTED.keys() & set(inspector.get_table_names(schema="public"))
