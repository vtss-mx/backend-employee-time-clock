"""La base de datos se organiza por esquemas de dominio (PostgreSQL)."""

from sqlalchemy import inspect

import app.models  # noqa: F401  (registra todas las tablas)
from app.core.database import Base, engine
from app.core.db_schemas import ALL_SCHEMAS, ATTENDANCE, AUTH, BILLING, BIOMETRICS, CATALOG, OPS, TENANCY, WORKFORCE
from app.models.catalog_seed import load_catalog_seed

EXPECTED = {
    AUTH: {
        "users",
        "user_avatars",
        "auth_sessions",
        "remembered_accounts",
        "rate_limit_counters",
        "passkeys",  # llaves de acceso (WebAuthn) de la persona y sus retos de un solo uso (antifraude fase 3, 0081)
        "passkey_challenges",
    },
    TENANCY: {"companies", "verification_policy", "company_api_keys", "company_api_key_scopes", "company_documents"},
    WORKFORCE: {
        "employees",
        "employee_qr_codes",
        "employee_devices",
        "employee_documents",
        "validators",
        "validator_devices",
        "departments",
        "department_managers",
        "work_sites",
        "site_kiosks",
        "shifts",
        "shift_assignments",
        "shift_sites",
        "shift_change_requests",
        "company_holidays",
        "employee_absences",
        "employee_workdays",
        "employee_status_events",
        "validator_status_events",
    },
    BIOMETRICS: {
        "face_enrollments",
        "face_enrollment_drafts",
        "face_enrollment_flags",
        "enrollment_voice_answers",
        "face_embeddings",
        "face_challenges",
        "capture_fingerprints",
        "capture_traces",
    },
    ATTENDANCE: {"verification_logs", "work_sessions", "work_breaks", "attendance_events"},
    OPS: {
        "error_reports",
        "error_occurrences",
        "face_attempt_metrics",
        "security_thresholds",
        "usage_daily",
        "usage_routes",
        "usage_users",
        "storage_snapshots",
        "daily_tasks",
        "storage_deletions",
        "storage_status",
        "perf_minutes",
        "perf_hours",
        "perf_days",
        "slow_request_alerts",
        # Antifraude fase 3 (migración 0081): deriva por ventana, fraude interno por empresa y bitácora del motor.
        "signal_drift",
        "company_fraud_weekly",
        "engine_versions",
        # Antifraude (migración 0062).
        "policy_changes",
        "risk_assessments",
        "fraud_cases",
        "fraud_case_attempts",
        "fraud_case_events",
        "fraud_evidence",
        "attack_signatures",
        "risk_signal_stats",
    },
    BILLING: {
        "plans",
        "headcount_days",
        "charges",
        "charge_lines",
        "payments",
        "payment_allocations",
    },
    # Cada tabla del catálogo tiene sus registros en alembic/seed/catalogs.json (y viceversa); sus textos en los demás
    # idiomas, en `translations` (alembic/seed/catalogs.<idioma>.json).
    CATALOG: set(load_catalog_seed()) | {"translations"},
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
