"""Aislamiento entre empresas en la base de datos: la última barrera (seguridad por fila de PostgreSQL).

Decisión del dueño del producto (regla 14 del `AGENTS.md` raíz): por ninguna razón se mezclan los datos de
una empresa con los de otra. Las capas son, de afuera hacia adentro: la empresa sale SIEMPRE de la sesión o
de la llave (`CompanyScope`), cada repositorio filtra por `company_id`, las llaves foráneas entre tablas de
empresa son compuestas `(id, company_id)` y, al final, esta: **aunque el código olvidara un filtro, la base
no entrega ni acepta filas de otra empresa.**

Cómo funciona:

- Toda tabla de `TENANT_TABLES` tiene RLS habilitada y FORZADA (también para su dueño) con la política
  `tenant_isolation`: `company_id = NULLIF(current_setting('app.company_id', true), '')::int`, para leer y
  para escribir (`WITH CHECK`).
- Cada transacción declara de quién es AL EMPEZAR (evento `after_begin` de la sesión), según el alcance
  guardado en `Session.info`:
  * **empresa N** → `set_config('app.company_id', 'N', true)`;
  * **plataforma** → `set_config('role', DB_PLATFORM_ROLE, true)`: el rol SIN login y con `BYPASSRLS` (ADMIN,
    autenticación, mantenimiento, cobranza, consumo). La conexión de la API (`DB_APP_USER`) no puede saltarse
    la política por sí misma: solo cambiando a ese rol, y eso lo hacen únicamente `use_platform` y
    `crossing_tenants`, explícitos y fáciles de buscar;
  * **sin alcance** → nada: las tablas de empresa no devuelven filas y rechazan escrituras (falla cerrado).
- `true` en `set_config` = solo esta transacción: al confirmar o revertir, PostgreSQL lo olvida. Por eso es
  correcto tras PgBouncer en modo transacción (la conexión del servidor pasa a otro cliente sin heredar la
  empresa ni el rol; medido con 8 clientes sobre 4 conexiones reales: 0 fugas) y nunca se usa `SET` de sesión.
- Costo: UNA sentencia de ida y vuelta por transacción con alcance (≈0.13 ms tras PgBouncer). La política es
  una igualdad sobre `company_id` (`int4eq`, *leakproof*): con el `company_id = :empresa` que ya lleva cada
  consulta, PostgreSQL la reduce a un filtro de una sola vez (`One-Time Filter`) y los índices que empiezan
  por `company_id` se siguen usando igual.

En SQLite (pruebas) no hay RLS: el alcance solo se anota en la conexión, y `tests/conftest.py` falla si una
consulta toca una tabla de empresa sin alcance (la misma regla que aplica PostgreSQL), así la suite rápida
también detecta un camino que olvidó declararlo. En PostgreSQL la prueba real es `tests/test_tenant_isolation.py`
con el rol de la API (`./scripts/quality.sh --postgres`).

Agregar una tabla de empresa: su nombre en `TENANT_TABLES` (y en la migración que la crea, con su política),
`company_id NOT NULL`, llaves foráneas compuestas e índice que empiece por `company_id`
(`backend-employee-time-clock/AGENTS.md` §3.2). `tests/test_row_security.py` falla si una tabla con `company_id`
no está clasificada.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final, Literal

from sqlalchemy import Connection, Table, TextClause, event, text
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import settings
from app.core.sql_safety import sql_identifier, sql_literal

#: Alcance de la plataforma (cruza empresas a propósito).
PLATFORM: Final = "platform"
type Scope = int | Literal["platform"] | None

#: Llave del alcance en `Session.info` y en `Connection.info` (esta la lee la guarda de las pruebas).
SCOPE_KEY: Final = "row_scope"
#: Conexión de la transacción en curso de una sesión (para cambiar de alcance a mitad de ella).
_CONNECTION_KEY: Final = "row_scope_connection"
#: Nombre de la política de cada tabla de empresa (el mismo en migraciones y modelos).
POLICY: Final = "tenant_isolation"
#: Variable de la transacción con la empresa en curso.
COMPANY_SETTING: Final = "app.company_id"
#: Expresión de la política: sin empresa (NULL o '' tras una transacción anterior) no coincide ninguna fila.
POLICY_EXPRESSION: Final = f"company_id = NULLIF(current_setting('{COMPANY_SETTING}', true), '')::integer"

#: Tablas con datos de una empresa: `company_id NOT NULL`, RLS forzada y la política `tenant_isolation`.
TENANT_TABLES: Final = frozenset(
    {
        # Personal y su organización
        "workforce.employees",
        "workforce.departments",
        "workforce.department_managers",
        "workforce.validators",
        "workforce.validator_devices",
        "workforce.employee_devices",
        "workforce.employee_qr_codes",
        "workforce.employee_status_events",
        "workforce.employee_documents",
        "workforce.validator_status_events",
        "workforce.work_sites",
        "workforce.site_kiosks",
        "workforce.shifts",
        "workforce.shift_sites",
        "workforce.shift_assignments",
        "workforce.shift_change_requests",
        "workforce.company_holidays",
        "workforce.employee_absences",
        "workforce.employee_workdays",
        # Biometría (plantillas cifradas, registros, huellas de capturas)
        "biometrics.face_enrollments",
        "biometrics.face_enrollment_drafts",
        "biometrics.face_enrollment_flags",
        "biometrics.enrollment_voice_answers",
        "biometrics.face_embeddings",
        "biometrics.capture_fingerprints",
        "biometrics.capture_traces",
        # Asistencia
        "attendance.verification_logs",
        "attendance.work_sessions",
        "attendance.work_breaks",
        "attendance.attendance_events",
        # Configuración de la empresa
        "tenancy.verification_policy",
        "tenancy.company_api_keys",
        "tenancy.company_api_key_scopes",
        # Documentos de la empresa (la referencia de cada archivo cifrado en el bucket; migración 0075)
        "tenancy.company_documents",
        # Lo que la plataforma mide y cobra de cada empresa (solo lo leen el ADMIN y el mantenimiento)
        "ops.face_attempt_metrics",
        # Antifraude (migración 0062): la decisión de riesgo de cada intento, los casos que revisa el ADMIN con su
        # evidencia, la auditoría de la política y la línea base de cada señal
        "ops.risk_assessments",
        "ops.fraud_cases",
        "ops.fraud_case_attempts",
        "ops.fraud_case_events",
        "ops.fraud_evidence",
        "ops.policy_changes",
        "ops.risk_signal_stats",
        # Deriva de señales (antifraude fase 3, migración 0081): la tasa de casos y las revisiones aprobadas sin mirar
        # de cada empresa y ventana (solo las lee el ADMIN; las escribe el mantenimiento como plataforma)
        "ops.company_fraud_weekly",
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
    }
)

#: Tablas de la plataforma que tienen una columna `company_id` sin ser datos de UNA empresa (no llevan RLS).
PLATFORM_TABLES_WITH_COMPANY: Final = {
    "auth.users": "Identidad: la cuenta de un COMPANY o VALIDATOR dice a qué empresa pertenece; la de un EMPLOYEE "
    "no tiene empresa (trabaja en varias) y la autenticación la lee antes de saber la empresa",
    "auth.auth_sessions": "Identidad: la empresa que eligió el empleado en su sesión (se lee para autenticar)",
    "ops.error_occurrences": "Errores del sistema: la empresa es un dato del contexto del error, solo lo lee el ADMIN",
    "ops.attack_signatures": "Lista de bloqueo de la plataforma (decisión D6): solo huellas pHash de ataques "
    "confirmados; company_id es el alcance de la firma (NULL = toda la plataforma), nunca datos de la empresa. Cada "
    "intento facial la consulta (en memoria) sin importar su empresa",
}


def scope_of(db: Session) -> Scope:
    """Alcance actual de la sesión (None: ninguno)."""
    scope: Scope = db.info.get(SCOPE_KEY)
    return scope


def use_company(db: Session, company_id: int) -> None:
    """Desde la siguiente sentencia, la sesión solo ve y escribe filas de `company_id`."""
    _switch(db, int(company_id))


def use_platform(db: Session) -> None:
    """La sesión cruza empresas A PROPÓSITO (ADMIN, autenticación, mantenimiento, cobranza, consumo).
    Cada llamada es una decisión explícita: se busca con `grep use_platform` y se justifica donde ocurre."""
    _switch(db, PLATFORM)


def clear_scope(db: Session) -> None:
    """Sin alcance: las tablas de empresa no devuelven nada (lo de cada petición antes de autenticar)."""
    _switch(db, None)


@contextmanager
def crossing_tenants(db: Session) -> Iterator[None]:
    """Cruza empresas solo dentro del bloque y vuelve al alcance anterior al salir (también si falla).

    Para la única lectura entre empresas que necesita una petición de empresa (p. ej. "¿esta persona también
    trabaja en otra empresa?"): el bloque dice exactamente qué cruza y por qué."""
    previous = scope_of(db)
    _switch(db, PLATFORM)
    try:
        yield
    finally:
        _switch(db, previous)


def _switch(db: Session, scope: Scope) -> None:
    db.info[SCOPE_KEY] = scope
    connection: Connection | None = db.info.get(_CONNECTION_KEY)
    if connection is not None and not connection.closed:
        # A mitad de una transacción: aplica ya (si no, lo hará `after_begin` en la siguiente).
        apply_scope(connection, scope, switching=True)


#: Las sentencias que declaran el alcance (una por transacción). Constantes: la empresa y el rol viajan como
#: parámetros, nunca dentro del texto.
_SET_COMPANY: Final = text("SELECT set_config(:setting, :company, true)")
_LEAVE_PLATFORM_FOR_COMPANY: Final = text(
    "SELECT set_config('role', 'none', true), set_config(:setting, :company, true)"
)
_SET_PLATFORM: Final = text("SELECT set_config(:setting, '', true), set_config('role', :role, true)")
_CLEAR_COMPANY: Final = text("SELECT set_config(:setting, '', true)")
_CLEAR_SCOPE: Final = text("SELECT set_config('role', 'none', true), set_config(:setting, '', true)")


def _statement(scope: Scope, *, switching: bool) -> tuple[TextClause, dict[str, Any]] | None:
    """La sentencia que declara el alcance en la transacción (None: nada que declarar)."""
    if scope == PLATFORM:
        role = settings.platform_role
        if not role:
            # Sin rol de plataforma configurado la API ya se conecta con un rol que no está sujeto a la
            # política (instalación de desarrollo con el dueño); el arranque lo avisa (`bypasses_row_security`).
            return (_CLEAR_COMPANY, {"setting": COMPANY_SETTING}) if switching else None
        return (_SET_PLATFORM, {"setting": COMPANY_SETTING, "role": role})
    if scope is None:
        return (_CLEAR_SCOPE, {"setting": COMPANY_SETTING}) if switching else None
    company = {"setting": COMPANY_SETTING, "company": str(scope)}
    if switching and settings.platform_role:  # deja el rol de la plataforma que traía la transacción
        return (_LEAVE_PLATFORM_FOR_COMPANY, company)
    return (_SET_COMPANY, company)


def apply_scope(connection: Connection, scope: Scope, *, switching: bool = False) -> None:
    """Declara el alcance en la transacción en curso de `connection` (`switching`: cambia el de una transacción que
    ya tenía otro). Lo usa el evento `after_begin`; también `perf/db/explain.py` para medir con la política."""
    connection.info[SCOPE_KEY] = scope
    if connection.dialect.name != "postgresql":
        return
    statement = _statement(scope, switching=switching)
    if statement is not None:
        connection.execute(*statement)


@event.listens_for(Session, "after_begin")
def _declare_scope(session: Session, _transaction: SessionTransaction, connection: Connection) -> None:
    """Primera sentencia de cada transacción: de quién es (ver el docstring del módulo). Un SAVEPOINT usa la misma
    conexión que su transacción, así que anotarla otra vez no cambia nada."""
    session.info[_CONNECTION_KEY] = connection
    apply_scope(connection, scope_of(session))


@event.listens_for(Session, "after_transaction_end")
def _forget_connection(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        session.info.pop(_CONNECTION_KEY, None)


# ---------- DDL de la política (modelos: create_all de pruebas y de quality.sh) ----------


#: Comentario de `company_id` en cada tabla de empresa (el mismo en la migración 0056).
COMPANY_COMMENT: Final = (
    "Empresa dueña de la fila. Seguridad por fila (política tenant_isolation): solo se ve y se escribe en una "
    "transacción con app.company_id igual (o con el rol de la plataforma)."
)


def policy_ddl(qualified_table: str) -> list[str]:
    """Habilitar y forzar RLS, crear la política y comentar `company_id` de una tabla de empresa (la misma SQL de
    la migración 0056)."""
    table = sql_identifier(qualified_table)
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {POLICY} ON {table} USING ({POLICY_EXPRESSION}) WITH CHECK ({POLICY_EXPRESSION})",
        f"COMMENT ON COLUMN {table}.company_id IS {sql_literal(COMPANY_COMMENT)}",
    ]


def _create_policy(table: Table, connection: Connection, **_: Any) -> None:
    if connection.dialect.name == "postgresql":
        for statement in policy_ddl(f"{table.schema}.{table.name}"):
            connection.exec_driver_sql(statement)


def register_tenant_tables(tables: dict[str, Table]) -> None:
    """Cada tabla de empresa de los modelos crea su política al crearse (`create_all`)."""
    for name in TENANT_TABLES:
        event.listen(tables[name], "after_create", _create_policy)
