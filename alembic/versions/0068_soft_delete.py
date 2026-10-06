"""Borrado lógico (soft delete): todo lo que una persona elimina va a «Eliminados» y se puede restaurar durante un año

Decisión del dueño del producto: «todos los delete de la aplicación deben ser softdelete». Eliminar un registro lo
marca (`deleted_at`, `deleted_by` = correo literal de quien lo hizo) en lugar de borrarlo; desaparece de listados,
búsquedas, conteos y validaciones de datos únicos, su historial se conserva y «Restaurar» lo regresa. Pasados
`SOFT_DELETE_RETENTION_DAYS` (365, Ley Federal del Trabajo) el mantenimiento lo borra de verdad. Los datos biométricos y
las fotos de una persona se borran de verdad al eliminarla (LFPDPPP; regla 13). Mecanismo: `app/core/soft_delete.py`.

Tablas con borrado lógico (las 10 que tienen una eliminación iniciada por una persona): `tenancy.companies`,
`auth.users`, `workforce.employees`, `workforce.validators`, `workforce.departments`, `workforce.work_sites`,
`workforce.shifts`, `workforce.shift_assignments`, `workforce.company_holidays` y `workforce.employee_workdays`.

1. Columnas `deleted_at` (timestamptz) y `deleted_by` (varchar 255), nulas: agregarlas no reescribe ninguna tabla y la
   versión anterior de la API (aún en marcha durante el despliegue) no las lee ni las necesita.
2. Datos únicos solo entre lo VIGENTE (índices únicos parciales `WHERE deleted_at IS NULL`, decisión del dueño: un
   empleado nuevo puede usar el correo, número, RFC, CURP o NSS de uno eliminado; igual el correo de un validador, el
   RFC de una empresa, el nombre de un departamento, sitio o turno, la fecha de un festivo y el día laborable):
   `ix_users_email`, `uq_users_phone`, `ix_companies_rfc`, `uq_employees_company_number|rfc|curp|nss|user`,
   `uq_departments_company_name`, `uq_work_sites_company_name`, `uq_shifts_company_name`,
   `uq_company_holidays_company_date` y `uq_employee_workdays_employee_date` (los tres que eran restricciones
   —teléfono, festivo y día laborable— pasan a ser índices únicos parciales con el mismo nombre). Los destinos de las
   FK compuestas (`uq_*_id_company`) siguen completos.
3. Índices del camino caliente que nunca leen lo eliminado: la galería facial (`ix_employees_company_approved`, parcial
   con `deleted_at IS NULL`); los que además son el índice de una FK (deben ver TODAS las filas para borrar en cascada o
   revisar un RESTRICT) llevan `deleted_at` en el INCLUDE para resolver "solo vigentes" sin leer la tabla:
   `ix_employees_company_name`, `ix_employees_company_department` y `ix_shift_assignments_company_employee`.
4. Papelera y depuración: `ix_<tabla>_deleted` (`company_id, deleted_at, id`; en empresas `deleted_at, id` y en
   cuentas `deleted_at`), parcial `WHERE deleted_at IS NOT NULL`: solo guarda lo eliminado (pequeño, y nada de lo
   vigente lo toca al insertar o editar). `ix_employee_workdays_employee`: la FK del empleado, que el único parcial ya
   no cubre (la depuración de un empleado borra sus días laborables en cascada).
5. Catálogo: el motivo de cierre de sesión `ACCOUNT_DELETED` (es-MX y su traducción en-US).

Los índices se construyen con `CREATE INDEX CONCURRENTLY` fuera de la transacción (sin bloquear escrituras en tablas
grandes) y uno que cambia de definición se construye con nombre temporal, se borra el anterior y se renombra (patrón de
la 0047): ninguna consulta se queda sin índice y repetir tras una falla a la mitad es seguro (cada paso con IF EXISTS).

`downgrade` deja exactamente lo anterior, pero SOLO con la papelera vacía: con filas eliminadas, los únicos completos
no se podrían volver a crear (un dato reutilizado estaría dos veces) y quitar las columnas haría reaparecer lo
eliminado como vigente. Si hay filas en la papelera se detiene con un error que lo explica: primero se restauran o se
depuran (`python -m app.cli purge` con una retención menor), nunca se pierden en silencio. Las sesiones cerradas con el
motivo nuevo pasan al anterior más cercano (`ACCOUNT_DEACTIVATED`) antes de quitarlo del catálogo.

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-05 23:00:00
"""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG = "catalog"
LOCALE = "en-US"
_TMP = "_new"
LIVE = "deleted_at IS NULL"
DELETED = "deleted_at IS NOT NULL"

#: Tablas con borrado lógico (esquema, tabla).
TABLES: tuple[tuple[str, str], ...] = (
    ("tenancy", "companies"),
    ("auth", "users"),
    ("workforce", "employees"),
    ("workforce", "validators"),
    ("workforce", "departments"),
    ("workforce", "work_sites"),
    ("workforce", "shifts"),
    ("workforce", "shift_assignments"),
    ("workforce", "company_holidays"),
    ("workforce", "employee_workdays"),
)

#: El motivo nuevo de cierre de sesión (los mismos textos que `alembic/seed/catalogs*.json`).
REASON = {
    "code": "ACCOUNT_DELETED",
    "name": "Cuenta eliminada",
    "description": "La cuenta o su empresa se eliminó: se cierran sus sesiones.",
    "message": "Tu cuenta se eliminó. Contacta a tu empresa si crees que es un error.",
    "sort_order": 14,
    "active": True,
}
REASON_EN = {
    "name": "Account deleted",
    "description": "The account or its company was deleted: its sessions are closed.",
    "message": "Your account was deleted. Contact your company if you think this is a mistake.",
}


@dataclass(frozen=True)
class Ix:
    """Un índice: su nombre y su definición después de `ON tabla` (columnas, INCLUDE y WHERE)."""

    name: str
    schema: str
    table: str
    definition: str
    unique: bool = False

    def create(self, name: str | None = None) -> None:
        target = name or self.name
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {self.schema}.{target}")
        unique = "UNIQUE " if self.unique else ""
        op.execute(f"CREATE {unique}INDEX CONCURRENTLY {target} ON {self.schema}.{self.table} {self.definition}")

    def drop(self) -> None:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {self.schema}.{self.name}")

    def replace(self) -> None:
        """Construye este índice con un nombre temporal, borra el anterior del mismo nombre y renombra."""
        self.create(self.name + _TMP)
        self.drop()
        op.execute(f"ALTER INDEX {self.schema}.{self.name + _TMP} RENAME TO {self.name}")


@dataclass(frozen=True)
class Uq:
    """Una restricción única que pasa a ser un índice único parcial con el mismo nombre."""

    name: str
    schema: str
    table: str
    columns: str

    def to_partial(self) -> None:
        temporary = Ix(self.name + _TMP, self.schema, self.table, f"({self.columns}) WHERE {LIVE}", unique=True)
        temporary.create()
        op.execute(f"ALTER TABLE {self.schema}.{self.table} DROP CONSTRAINT IF EXISTS {self.name}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {self.schema}.{self.name}")
        op.execute(f"ALTER INDEX {self.schema}.{self.name + _TMP} RENAME TO {self.name}")

    def to_constraint(self) -> None:
        """La restricción completa de antes (su índice se construye sin bloquear y la restricción lo adopta)."""
        temporary = Ix(self.name + _TMP, self.schema, self.table, f"({self.columns})", unique=True)
        temporary.create()
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {self.schema}.{self.name}")
        op.execute(
            f"ALTER TABLE {self.schema}.{self.table} ADD CONSTRAINT {self.name} UNIQUE USING INDEX {self.name + _TMP}"
        )


def _trash(schema: str, table: str, columns: str = "company_id, deleted_at, id") -> Ix:
    return Ix(f"ix_{table}_deleted", schema, table, f"({columns}) WHERE {DELETED}")


#: Índices nuevos (el downgrade los borra).
NEW: tuple[Ix, ...] = (
    _trash("tenancy", "companies", "deleted_at, id"),
    _trash("auth", "users", "deleted_at"),
    _trash("workforce", "employees"),
    _trash("workforce", "validators"),
    _trash("workforce", "departments"),
    _trash("workforce", "work_sites"),
    _trash("workforce", "shifts"),
    _trash("workforce", "shift_assignments"),
    _trash("workforce", "company_holidays"),
    _trash("workforce", "employee_workdays"),
    Ix("ix_employee_workdays_employee", "workforce", "employee_workdays", "(employee_id)"),
)


def _unique(name: str, schema: str, table: str, columns: str) -> tuple[Ix, Ix]:
    """(parcial de lo vigente, completo de antes) de un índice único que conserva su nombre."""
    return (
        Ix(name, schema, table, f"({columns}) WHERE {LIVE}", unique=True),
        Ix(name, schema, table, f"({columns})", unique=True),
    )


#: (nueva definición, definición anterior) de los índices que conservan su nombre.
REPLACED: tuple[tuple[Ix, Ix], ...] = (
    _unique("ix_users_email", "auth", "users", "email"),
    _unique("ix_companies_rfc", "tenancy", "companies", "rfc"),
    _unique("uq_employees_company_number", "workforce", "employees", "company_id, employee_number"),
    _unique("uq_employees_company_rfc", "workforce", "employees", "company_id, rfc"),
    _unique("uq_employees_company_curp", "workforce", "employees", "company_id, curp"),
    _unique("uq_employees_company_nss", "workforce", "employees", "company_id, nss"),
    _unique("uq_employees_company_user", "workforce", "employees", "company_id, user_id"),
    _unique("uq_departments_company_name", "workforce", "departments", "company_id, lower((name)::text)"),
    _unique("uq_work_sites_company_name", "workforce", "work_sites", "company_id, lower((name)::text)"),
    _unique("uq_shifts_company_name", "workforce", "shifts", "company_id, lower((name)::text)"),
    (
        Ix(
            "ix_employees_company_approved",
            "workforce",
            "employees",
            "(company_id, id) WHERE active IS TRUE AND face_status = 'APPROVED' AND deleted_at IS NULL",
        ),
        Ix(
            "ix_employees_company_approved",
            "workforce",
            "employees",
            "(company_id, id) WHERE active IS TRUE AND face_status = 'APPROVED'",
        ),
    ),
    (
        Ix(
            "ix_employees_company_name",
            "workforce",
            "employees",
            "(company_id, last_name, first_name, id) INCLUDE (deleted_at)",
        ),
        Ix("ix_employees_company_name", "workforce", "employees", "(company_id, last_name, first_name, id)"),
    ),
    (
        Ix(
            "ix_employees_company_department",
            "workforce",
            "employees",
            "(company_id, department_id, last_name, first_name, id) INCLUDE (deleted_at) WHERE department_id IS NOT NULL",
        ),
        Ix(
            "ix_employees_company_department",
            "workforce",
            "employees",
            "(company_id, department_id, last_name, first_name, id) WHERE department_id IS NOT NULL",
        ),
    ),
    (
        Ix(
            "ix_shift_assignments_company_employee",
            "workforce",
            "shift_assignments",
            "(company_id, employee_id, valid_from) INCLUDE (valid_to, shift_id, deleted_at)",
        ),
        Ix(
            "ix_shift_assignments_company_employee",
            "workforce",
            "shift_assignments",
            "(company_id, employee_id, valid_from) INCLUDE (valid_to, shift_id)",
        ),
    ),
)

#: Restricciones únicas que pasan a ser índices únicos parciales con el mismo nombre.
CONSTRAINTS: tuple[Uq, ...] = (
    Uq("uq_users_phone", "auth", "users", "phone"),
    Uq("uq_company_holidays_company_date", "workforce", "company_holidays", "company_id, holiday_date"),
    Uq("uq_employee_workdays_employee_date", "workforce", "employee_workdays", "employee_id, work_date"),
)


def _reason() -> None:
    """El motivo `ACCOUNT_DELETED` y su traducción (idempotente: una base nueva ya lo trae del seed)."""
    columns = ", ".join(REASON)
    values = ", ".join(f":{key}" for key in REASON)
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f"INSERT INTO {CATALOG}.session_revocation_reasons ({columns}) VALUES ({values}) "
            "ON CONFLICT (code) DO NOTHING"
        ),
        REASON,
    )
    for field, text in REASON_EN.items():
        bind.execute(
            sa.text(
                f"INSERT INTO {CATALOG}.translations (catalog, code, locale, field, text) "
                "VALUES ('session_revocation_reasons', :code, :locale, :field, :text) "
                "ON CONFLICT (catalog, code, locale, field) DO UPDATE SET text = EXCLUDED.text"
            ),
            {"code": REASON["code"], "locale": LOCALE, "field": field, "text": text},
        )


def upgrade() -> None:
    for schema, table in TABLES:
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True), schema=schema)
        op.add_column(table, sa.Column("deleted_by", sa.String(length=255), nullable=True), schema=schema)
    _reason()
    # CONCURRENTLY no puede ir dentro de una transacción: cada sentencia se confirma sola.
    with op.get_context().autocommit_block():
        # Primero lo nuevo (ninguna consulta se queda sin índice) y después lo que cambia de definición.
        for index in NEW:
            index.create()
        for new, _ in REPLACED:
            new.replace()
        for constraint in CONSTRAINTS:
            constraint.to_partial()


def _ensure_empty_trash() -> None:
    """Con filas en la papelera el downgrade no puede dejar lo anterior (ver el docstring): se detiene sin cambiar nada."""
    bind = op.get_bind()
    pending = {
        f"{schema}.{table}": count
        for schema, table in TABLES
        if (count := bind.execute(sa.text(f"SELECT count(*) FROM {schema}.{table} WHERE {DELETED}")).scalar())
    }
    if pending:
        raise RuntimeError(
            "No se puede revertir la migración 0068 con registros en «Eliminados» "
            f"({', '.join(f'{name}: {count}' for name, count in sorted(pending.items()))}): restáuralos o depúralos "
            "primero (python -m app.cli purge con una retención menor). Nada cambió."
        )


def downgrade() -> None:
    _ensure_empty_trash()
    with op.get_context().autocommit_block():
        for constraint in CONSTRAINTS:
            constraint.to_constraint()
        for _, previous in REPLACED:
            previous.replace()
        for index in NEW:
            index.drop()
    op.execute(
        "UPDATE auth.auth_sessions SET revoked_reason = 'ACCOUNT_DEACTIVATED' WHERE revoked_reason = 'ACCOUNT_DELETED'"
    )
    op.execute(
        f"DELETE FROM {CATALOG}.translations WHERE catalog = 'session_revocation_reasons' AND code = 'ACCOUNT_DELETED'"
    )
    op.execute(f"DELETE FROM {CATALOG}.session_revocation_reasons WHERE code = 'ACCOUNT_DELETED'")
    for schema, table in reversed(TABLES):
        op.drop_column(table, "deleted_by", schema=schema)
        op.drop_column(table, "deleted_at", schema=schema)
