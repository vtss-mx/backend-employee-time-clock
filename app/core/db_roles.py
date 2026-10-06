"""Roles de la base con mínimo privilegio (regla 15 del `AGENTS.md` raíz; README "Roles, privilegios y seguridad
por fila").

- dueño (`POSTGRES_USER`, con login): todo (DDL). Solo lo usan las migraciones (`migrate`), este aprovisionamiento
  y los respaldos.
- API (`DB_APP_USER`, con login): SELECT/INSERT/UPDATE/DELETE en los esquemas de dominio (catálogos: solo SELECT).
  Sin DDL, sin superusuario y SIN `BYPASSRLS`: la seguridad por fila le aplica.
- plataforma (`DB_PLATFORM_ROLE`, sin login): lo mismo que la API, con `BYPASSRLS`. La API cambia a él solo dentro
  de una transacción del código de la plataforma (`row_security.use_platform`).
- solo lectura (`DB_READONLY_USER`, con login): SELECT, sujeto a la seguridad por fila (para ver una empresa:
  `SET app.company_id = N`).

Se ejecuta en cada despliegue después de las migraciones (`entrypoint.sh migrate` → `python -m app.cli db roles`),
con la conexión directa del dueño: es idempotente (crea lo que falta, vuelve a fijar contraseñas, atributos,
tiempos límite y permisos), así rotar una contraseña es cambiarla en `.env` y desplegar. Las contraseñas viajan
ya convertidas en su verificador SCRAM-SHA-256 (`scram_verifier`): nunca en claro, ni en la red ni en los logs
de PostgreSQL.

Los permisos se conceden tabla por tabla a las tablas normales y a las PADRE de las particionadas, nunca a las
particiones (las crea `ops.ensure_partitions` con el dueño): la seguridad por fila vive en la tabla padre, así que
nadie puede leer una partición directamente para saltársela. Por eso no se usan privilegios por omisión
(`ALTER DEFAULT PRIVILEGES`): una tabla nueva recibe sus permisos en el aprovisionamiento del mismo despliegue.
"""

import base64
import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings, settings
from app.core.db_schemas import ALL_SCHEMAS, CATALOG
from app.core.sql_safety import sql_identifier, sql_literal

logger = logging.getLogger(__name__)

#: Nombre válido de un rol (sin comillas: se escribe tal cual en la SQL).
_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
#: Iteraciones del verificador SCRAM (las de PostgreSQL por omisión).
SCRAM_ITERATIONS: Final = 4096
#: Funciones que ejecuta el rol de la plataforma (y nadie más).
PLATFORM_FUNCTIONS: Final = (
    "ops.ensure_partitions(text, date, integer, date)",
    # Las consultas que más consumen la base (pantalla "Rendimiento" del ADMIN; migración 0063).
    "ops.top_statements(text, integer, integer)",
)
#: Candado de transacción del aprovisionamiento (junto a los de migraciones 7_420_031 y mantenimiento 7_420_032):
#: varias réplicas que migran a la vez (RUN_MIGRATIONS=1) aprovisionan una tras otra, nunca dos `CREATE ROLE`
#: o dos `GRANT` sobre lo mismo a la vez (PostgreSQL respondería "ya existe" o "actualizado en paralelo").
PROVISION_LOCK_KEY: Final = 7_420_033


def scram_verifier(password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """Verificador SCRAM-SHA-256 de una contraseña (RFC 5802/7677), el mismo formato que guarda PostgreSQL.

    `ALTER ROLE ... PASSWORD '<verificador>'` lo guarda sin conocer la contraseña: no queda en claro en la red,
    en `pg_stat_statements` ni en el log de sentencias."""
    salt = salt or os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    b64 = base64.b64encode
    return f"SCRAM-SHA-256${iterations}:{b64(salt).decode()}${b64(stored_key).decode()}:{b64(server_key).decode()}"


def role_name(value: str) -> str:
    """Un nombre de rol (o de base) seguro para escribirse en la SQL: minúsculas, números y `_`; si no, error
    claro (nunca se arma SQL con un nombre que habría que entrecomillar)."""
    if not _ROLE_NAME.fullmatch(value):
        raise ValueError(f"Nombre inválido: {value!r} (solo minúsculas, números y _, hasta 63)")
    return value


@dataclass(frozen=True)
class RolePlan:
    """Los roles que se aprovisionan con la configuración dada."""

    app: str
    app_password: str
    platform: str
    readonly: str | None
    readonly_password: str
    statement_timeout_ms: int
    lock_timeout_ms: int
    idle_in_transaction_ms: int

    @classmethod
    def from_settings(cls, config: Settings = settings) -> RolePlan:
        if not config.DB_APP_PASSWORD:
            raise ValueError("DB_APP_PASSWORD vacía: sin el usuario de la API no hay roles que aprovisionar")
        return cls(
            app=role_name(config.DB_APP_USER),
            app_password=config.DB_APP_PASSWORD,
            platform=role_name(config.DB_PLATFORM_ROLE),
            readonly=role_name(config.DB_READONLY_USER) if config.DB_READONLY_PASSWORD else None,
            readonly_password=config.DB_READONLY_PASSWORD,
            statement_timeout_ms=config.DB_STATEMENT_TIMEOUT_MS,
            lock_timeout_ms=config.DB_LOCK_TIMEOUT_MS,
            idle_in_transaction_ms=config.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS,
        )


def _ensure_role(conn: Connection, name: str, attributes: str, password: str | None) -> None:
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": name}).scalar()
    if not exists:
        conn.exec_driver_sql(f"CREATE ROLE {sql_identifier(name)}")
    # `ALTER ROLE` no acepta parámetros: el verificador (lo arma `scram_verifier`, base64) va como literal escapada.
    secret = f" PASSWORD {sql_literal(scram_verifier(password))}" if password else ""
    conn.exec_driver_sql(f"ALTER ROLE {sql_identifier(name)} WITH {attributes}{secret}")


def _limits(conn: Connection, role: str, plan: RolePlan) -> None:
    """Tiempos límite de las sesiones del rol (al conectarse: también tras PgBouncer, que abre sus conexiones con
    este usuario). El mantenimiento apaga el de transacción inactiva solo en su candado (`SET LOCAL`)."""
    for setting, value in (
        ("statement_timeout", plan.statement_timeout_ms),
        ("lock_timeout", plan.lock_timeout_ms),
        ("idle_in_transaction_session_timeout", plan.idle_in_transaction_ms),
    ):
        conn.exec_driver_sql(f"ALTER ROLE {sql_identifier(role)} SET {sql_identifier(setting)} = {int(value)}")


def _tables(conn: Connection, schema: str) -> list[str]:
    """Tablas normales y padres de particiones del esquema (nunca las particiones)."""
    rows = conn.execute(
        text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relkind IN ('r', 'p') AND NOT c.relispartition ORDER BY c.relname"
        ),
        {"schema": schema},
    )
    return [f"{schema}.{name}" for (name,) in rows]


def grant(conn: Connection, plan: RolePlan) -> None:
    """Permisos de cada rol sobre la base, los esquemas, las tablas (no las particiones), las secuencias y la
    función de particiones (idempotente: también lo usan las pruebas tras crear sus tablas)."""
    writers = sql_identifier(plan.app, plan.platform)
    readers = sql_identifier(plan.app, plan.platform, plan.readonly) if plan.readonly else writers
    database = sql_identifier(str(conn.execute(text("SELECT current_database()")).scalar_one()))
    conn.exec_driver_sql(f"REVOKE ALL ON DATABASE {database} FROM PUBLIC")
    conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {database} TO {readers}")
    # `public` solo guarda las extensiones (pg_trgm, btree_gin): se usan sus funciones, nadie crea nada ahí.
    conn.exec_driver_sql("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {readers}")
    for schema in ALL_SCHEMAS:
        conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA {sql_identifier(schema)} FROM PUBLIC")
        conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA {sql_identifier(schema)} TO {readers}")
        writes = "SELECT" if schema == CATALOG else "SELECT, INSERT, UPDATE, DELETE"
        for table in _tables(conn, schema):  # nombres del catálogo de la base: se revisan y entrecomillan igual
            conn.exec_driver_sql(f"REVOKE ALL ON {sql_identifier(table)} FROM PUBLIC")
            conn.exec_driver_sql(f"GRANT {writes} ON {sql_identifier(table)} TO {writers}")
            if plan.readonly:
                conn.exec_driver_sql(f"GRANT SELECT ON {sql_identifier(table)} TO {sql_identifier(plan.readonly)}")
        conn.exec_driver_sql(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {sql_identifier(schema)} TO {writers}")
    for function in PLATFORM_FUNCTIONS:
        if conn.execute(text("SELECT to_regprocedure(:f) IS NOT NULL"), {"f": function}).scalar():
            conn.exec_driver_sql(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
            conn.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {function} TO {sql_identifier(plan.platform)}")


def _observability(conn: Connection) -> None:
    """`pg_stat_statements` (qué consultas consumen la base) si el servidor lo carga. De mejor esfuerzo, en su
    propio punto de guardado: una base administrada sin permiso para crear la extensión sigue (se avisa)."""
    loaded = str(conn.execute(text("SELECT current_setting('shared_preload_libraries')")).scalar() or "")
    if "pg_stat_statements" not in loaded:
        logger.warning(
            "PostgreSQL no carga pg_stat_statements (shared_preload_libraries): sin estadísticas por consulta"
        )
        return
    try:
        with conn.begin_nested():
            conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public")
    except DBAPIError as exc:
        logger.warning("No se pudo crear pg_stat_statements (%s): se sigue sin él", type(exc.orig).__name__)


def provision(conn: Connection, plan: RolePlan) -> None:
    """Crea o pone al día los roles y sus permisos (idempotente). Corre en UNA transacción: o queda todo o nada."""
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": PROVISION_LOCK_KEY})
    _ensure_role(
        conn,
        plan.platform,
        "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS NOINHERIT",
        None,
    )
    _ensure_role(
        conn,
        plan.app,
        "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT",
        plan.app_password,
    )
    # La API puede CAMBIAR al rol de la plataforma (SET ROLE), pero no hereda sus permisos ni su BYPASSRLS.
    if int(conn.execute(text("SHOW server_version_num")).scalar_one()) >= 160_000:
        conn.exec_driver_sql(
            f"GRANT {sql_identifier(plan.platform)} TO {sql_identifier(plan.app)} WITH INHERIT FALSE, SET TRUE"
        )
    else:  # PostgreSQL 15: la membresía hereda permisos (iguales a los suyos); BYPASSRLS nunca se hereda
        conn.exec_driver_sql(f"GRANT {sql_identifier(plan.platform)} TO {sql_identifier(plan.app)}")
    _limits(conn, plan.app, plan)
    if plan.readonly:
        _ensure_role(
            conn,
            plan.readonly,
            "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
            plan.readonly_password,
        )
        _limits(conn, plan.readonly, plan)
        conn.exec_driver_sql(f"ALTER ROLE {sql_identifier(plan.readonly)} SET default_transaction_read_only = on")
    grant(conn, plan)
    _observability(conn)
