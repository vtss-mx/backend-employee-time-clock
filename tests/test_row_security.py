"""Seguridad por fila (`app/core/row_security.py`) y la estructura que la sostiene (regla 14 del AGENTS.md raíz).

- Cada tabla con `company_id` está clasificada: de empresa (`TENANT_TABLES`: `company_id NOT NULL`, RLS y su
  política) o de la plataforma con su motivo. Una tabla nueva sin clasificar falla aquí.
- Toda llave foránea entre tablas de empresa lleva `company_id` (compuesta): la base no liga filas de dos empresas.
- Cada transacción declara su alcance al empezar (empresa, plataforma o ninguno) con la sentencia correcta, también
  al cambiar a mitad de una transacción y al cruzar empresas de forma explícita (`crossing_tenants`).
- La guarda de la suite rápida (SQLite, `tests/conftest.py`) aplica la misma regla que PostgreSQL.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import ForeignKeyConstraint, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.core import row_security
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.core.partitions import PARTITIONED
from app.core.row_security import (
    COMPANY_SETTING,
    PLATFORM,
    PLATFORM_TABLES_WITH_COMPANY,
    SCOPE_KEY,
    TENANT_TABLES,
    apply_scope,
    clear_scope,
    crossing_tenants,
    policy_ddl,
    scope_of,
    use_company,
    use_platform,
)
from app.models import Employee

#: FK entre tablas de empresa que no incluyen `company_id` y por qué es seguro.
FK_EXCEPTIONS = {
    # La muestra y su registro son del MISMO empleado; el empleado ya está atado a la empresa por la FK compuesta
    # `fk_face_embeddings_employee_company`.
    "fk_face_embeddings_enrollment_employee",
}


def _company_tables() -> dict[str, object]:
    return {name: table for name, table in Base.metadata.tables.items() if "company_id" in table.c}


def test_every_table_with_company_id_is_classified():
    tables = _company_tables()
    unclassified = set(tables) - TENANT_TABLES - set(PLATFORM_TABLES_WITH_COMPANY)
    assert unclassified == set(), "Clasifícala en row_security.TENANT_TABLES o PLATFORM_TABLES_WITH_COMPANY"
    assert set(tables) >= TENANT_TABLES
    assert set(PLATFORM_TABLES_WITH_COMPANY) <= set(tables) and not TENANT_TABLES & set(PLATFORM_TABLES_WITH_COMPANY)


def test_tenant_tables_require_their_company():
    nullable = [name for name in TENANT_TABLES if Base.metadata.tables[name].c.company_id.nullable]
    assert nullable == []


def test_relations_between_tenant_tables_carry_the_company():
    """Cada FK de una tabla de empresa hacia otra tabla de empresa incluye `company_id` en ambos lados."""
    loose = []
    for name in TENANT_TABLES:
        for constraint in Base.metadata.tables[name].constraints:
            if not isinstance(constraint, ForeignKeyConstraint) or constraint.name in FK_EXCEPTIONS:
                continue
            target = constraint.elements[0].column.table
            if f"{target.schema}.{target.name}" not in TENANT_TABLES:
                continue  # hacia la empresa, una cuenta o un catálogo
            pairs = {(e.parent.name, e.column.name) for e in constraint.elements}
            if ("company_id", "company_id") not in pairs:
                loose.append(constraint.name)
    assert loose == []


def test_partitioned_tables_keep_the_partition_key_in_their_primary_key_on_postgresql():
    for name, spec in PARTITIONED.items():
        ddl = str(CreateTable(Base.metadata.tables[name]).compile(dialect=postgresql.dialect()))
        primary = ddl.split("PRIMARY KEY (", 1)[1].split(")", 1)[0]
        assert spec.column in primary and f"PARTITION BY RANGE ({spec.column})" in ddl, name


def test_policy_ddl_enables_forces_and_filters_by_company():
    statements = policy_ddl("workforce.employees")
    assert statements[:2] == [
        "ALTER TABLE workforce.employees ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE workforce.employees FORCE ROW LEVEL SECURITY",
    ]
    assert f"current_setting('{COMPANY_SETTING}', true)" in statements[2] and "WITH CHECK" in statements[2]
    assert statements[3].startswith("COMMENT ON COLUMN workforce.employees.company_id IS")


class _FakeConnection:
    """Conexión de PostgreSQL simulada: anota lo que se ejecuta."""

    def __init__(self, dialect: str = "postgresql") -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self.info: dict = {}
        self.executed: list[tuple[str, dict]] = []
        self.closed = False

    def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))

    def exec_driver_sql(self, statement):
        self.executed.append((statement, {}))


@pytest.mark.parametrize(
    ("scope", "switching", "role", "expected"),
    [
        (7, False, "plataforma", ("SELECT set_config(:setting, :company, true)", {"company": "7"})),
        (7, True, "plataforma", ("SELECT set_config('role', 'none', true), set_config(:setting, :company, true)", {})),
        (7, True, "", ("SELECT set_config(:setting, :company, true)", {"company": "7"})),
        (PLATFORM, False, "plataforma", ("set_config('role', :role, true)", {"role": "plataforma"})),
        (PLATFORM, False, "", None),
        (PLATFORM, True, "", ("SELECT set_config(:setting, '', true)", {})),
        (None, False, "plataforma", None),
        (None, True, "plataforma", ("set_config('role', 'none', true)", {})),
    ],
    ids=[
        "empresa",
        "empresa-tras-plataforma",
        "empresa-sin-rol",
        "plataforma",
        "plataforma-con-el-dueño",
        "plataforma-a-mitad-con-el-dueño",
        "sin-alcance",
        "sin-alcance-a-mitad",
    ],
)
def test_each_scope_is_declared_with_one_statement(monkeypatch, scope, switching, role, expected):
    monkeypatch.setattr(settings, "DB_PLATFORM_ROLE", role or "timeclock_platform")
    monkeypatch.setattr(settings, "DB_APP_PASSWORD", "x" if role else "")
    if role:
        monkeypatch.setattr(settings, "DB_PLATFORM_ROLE", role)
    connection = _FakeConnection()
    apply_scope(connection, scope, switching=switching)
    assert connection.info[SCOPE_KEY] == scope
    if expected is None:
        assert connection.executed == []
        return
    ((statement, params),) = connection.executed
    assert expected[0] in statement
    assert expected[1].items() <= params.items()


def test_sqlite_only_annotates_the_scope():
    connection = _FakeConnection("sqlite")
    apply_scope(connection, 3)
    assert connection.info[SCOPE_KEY] == 3 and connection.executed == []


def test_a_session_declares_its_scope_at_each_transaction_and_when_it_changes():
    with SessionLocal(info={SCOPE_KEY: None}) as db:
        assert scope_of(db) is None
        use_company(db, 1)
        db.execute(select(Employee.id).limit(1))  # empieza la transacción: la declara
        assert db.connection().info[SCOPE_KEY] == 1
        with crossing_tenants(db):  # a mitad de la transacción: cambia ya, y vuelve al salir
            assert db.connection().info[SCOPE_KEY] == PLATFORM
        assert db.connection().info[SCOPE_KEY] == 1
        db.commit()
        use_platform(db)  # sin transacción: la siguiente empieza con el nuevo
        db.execute(text("SELECT 1"))
        assert db.connection().info[SCOPE_KEY] == PLATFORM
        clear_scope(db)
        assert db.connection().info[SCOPE_KEY] is None


def test_crossing_tenants_restores_the_scope_even_if_it_fails():
    with SessionLocal(info={SCOPE_KEY: 5}) as db:
        with pytest.raises(RuntimeError), crossing_tenants(db):
            assert scope_of(db) == PLATFORM
            raise RuntimeError("falla dentro del cruce")
        assert scope_of(db) == 5


def test_the_fast_suite_refuses_a_tenant_query_without_scope():
    """La guarda de tests/conftest.py: lo que PostgreSQL no dejaría ver, aquí hace fallar la prueba."""
    if engine.dialect.name != "sqlite":
        pytest.skip("En PostgreSQL lo impide la propia base (tests/test_tenant_isolation.py)")
    with SessionLocal(info={SCOPE_KEY: None}) as db, pytest.raises(AssertionError, match="sin alcance"):
        db.execute(select(Employee.id))


def test_models_create_the_policy_only_on_postgresql():
    table = Base.metadata.tables["workforce.departments"]
    postgres, sqlite = _FakeConnection(), _FakeConnection("sqlite")
    row_security._create_policy(table, postgres)
    row_security._create_policy(table, sqlite)
    assert [s for s, _ in postgres.executed] == policy_ddl("workforce.departments") and sqlite.executed == []
