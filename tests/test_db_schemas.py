"""La base de datos se organiza por esquemas de dominio (PostgreSQL)."""

from sqlalchemy import inspect

import app.models  # noqa: F401  (registra todas las tablas)
from app.core.database import Base, engine
from app.core.db_schemas import ALL_SCHEMAS, ATTENDANCE, AUTH, BIOMETRICS, TENANCY, WORKFORCE

EXPECTED = {
    AUTH: {"users", "auth_sessions", "remembered_accounts", "rate_limit_counters"},
    TENANCY: {"companies", "verification_policy"},
    WORKFORCE: {"employees", "employee_qr_codes", "validators"},
    BIOMETRICS: {"face_enrollments", "face_embeddings", "face_challenges"},
    ATTENDANCE: {"verification_logs"},
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
