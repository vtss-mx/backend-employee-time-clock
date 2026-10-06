"""Aislamiento entre empresas en la estructura: `company_id` en toda tabla de empresa y llaves foráneas compuestas

Decisión del dueño del producto (regla 14 del `AGENTS.md` raíz): por ninguna razón se mezclan los datos de una
empresa con los de otra. La auditoría de esta versión encontró tablas de empresa SIN `company_id` (su empresa
solo se deducía uniendo con otra tabla) y relaciones entre tablas de empresa por id solamente: con ellas, un
error del código podía ligar una fila a la de otra empresa sin que la base lo impidiera. Aquí:

- `company_id NOT NULL` (copiado de su fila padre) en `workforce.employee_qr_codes`, `biometrics.face_embeddings`,
  `biometrics.face_enrollment_flags`, `tenancy.company_api_key_scopes`, `billing.charge_lines` y
  `billing.payment_allocations`; y obligatorio en `attendance.verification_logs` (hasta hoy aceptaba NULL: se
  completa con la empresa de su empleado o de la cuenta que lo hizo).
- Llaves foráneas COMPUESTAS `(x_id, company_id)` → `(id, company_id)` en lugar de las de solo id: QR, muestras
  faciales y marcas de registro → su empleado o registro; dispositivos → su validador; permisos → su llave;
  líneas y aplicaciones de pago → su cargo y su pago (la base ya no puede aplicar el dinero de una empresa a la
  deuda de otra); descansos y registros de asistencia → su jornada; bitácora y registros → su empleado. Sus
  destinos `uq_<tabla>_id_company`. Se quitan dos FK redundantes (las cubre una compuesta).
- `ix_face_embeddings_company_model` (company_id, model_name) INCLUDE (id, employee_id) WHERE active: la galería
  facial de una empresa (en CADA identificación 1:N) ya no recorre las muestras de toda la plataforma (era el
  pendiente "galería facial sin company_id" del README).

**Antes de cambiar nada revisa que ninguna fila mezcle empresas** y, si alguna lo hace, se detiene con el conteo de
cada caso: no se borra ni se corrige ningún dato sin que el dueño lo decida.

Tablas grandes sin bloquear la operación (§3.1.13): índices `CONCURRENTLY` y llaves foráneas `NOT VALID` +
`VALIDATE` (la validación no frena lecturas ni escrituras) fuera de la transacción; repetible tras una falla a la
mitad (cada paso revisa si ya se hizo). Las réplicas con la versión anterior siguen funcionando mientras se
aplica: lo que escriben ya cumple todo esto (la empresa de su fila padre). La seguridad por fila llega en `0056`.

Revision ID: 0054
Revises: 0053
Create Date: 2026-10-05 22:00:00
"""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@dataclass(frozen=True)
class CompanyKey:
    """Una FK compuesta `(column, company_id)` → `target(id, company_id)` y la de solo id que reemplaza."""

    table: str
    column: str
    target: str
    ondelete: str
    old: str | None

    @property
    def name(self) -> str:
        return f"fk_{self.table.split('.')[1]}_{self.column.removesuffix('_id')}_company"


#: Tablas a las que se agrega `company_id`: (tabla, columna padre, tabla padre).
NEW_COLUMNS = (
    ("workforce.employee_qr_codes", "employee_id", "workforce.employees"),
    ("biometrics.face_embeddings", "employee_id", "workforce.employees"),
    ("biometrics.face_enrollment_flags", "enrollment_id", "biometrics.face_enrollments"),
    ("tenancy.company_api_key_scopes", "api_key_id", "tenancy.company_api_keys"),
    ("billing.charge_lines", "charge_id", "billing.charges"),
    ("billing.payment_allocations", "payment_id", "billing.payments"),
)
#: Destinos nuevos de FK compuestas (tablas chicas; `work_sessions` va aparte, CONCURRENTLY).
TARGETS = (
    "workforce.validators",
    "biometrics.face_enrollments",
    "tenancy.company_api_keys",
    "billing.charges",
    "billing.payments",
)
KEYS = (
    CompanyKey(
        "workforce.employee_qr_codes",
        "employee_id",
        "workforce.employees",
        "CASCADE",
        "fk_employee_qr_codes_employee_id_employees",
    ),
    CompanyKey(
        "biometrics.face_embeddings",
        "employee_id",
        "workforce.employees",
        "CASCADE",
        "fk_face_embeddings_employee_id_employees",
    ),
    CompanyKey(
        "biometrics.face_enrollment_flags",
        "enrollment_id",
        "biometrics.face_enrollments",
        "CASCADE",
        "fk_face_enrollment_flags_enrollment_id_face_enrollments",
    ),
    CompanyKey(
        "workforce.validator_devices",
        "validator_id",
        "workforce.validators",
        "CASCADE",
        "fk_validator_devices_validator_id_validators",
    ),
    CompanyKey(
        "tenancy.company_api_key_scopes",
        "api_key_id",
        "tenancy.company_api_keys",
        "CASCADE",
        "fk_company_api_key_scopes_api_key_id_company_api_keys",
    ),
    CompanyKey("billing.charge_lines", "charge_id", "billing.charges", "CASCADE", "fk_charge_lines_charge_id_charges"),
    CompanyKey(
        "billing.payment_allocations",
        "payment_id",
        "billing.payments",
        "CASCADE",
        "fk_payment_allocations_payment_id_payments",
    ),
    CompanyKey(
        "billing.payment_allocations",
        "charge_id",
        "billing.charges",
        "CASCADE",
        "fk_payment_allocations_charge_id_charges",
    ),
    CompanyKey(
        "attendance.work_breaks",
        "session_id",
        "attendance.work_sessions",
        "CASCADE",
        "fk_work_breaks_session_id_work_sessions",
    ),
    CompanyKey(
        "attendance.verification_logs",
        "employee_id",
        "workforce.employees",
        "CASCADE",
        "fk_verification_logs_employee_id_employees",
    ),
    CompanyKey(
        "attendance.attendance_events",
        "employee_id",
        "workforce.employees",
        "CASCADE",
        "fk_attendance_events_employee_id_employees",
    ),
    CompanyKey(
        "attendance.attendance_events",
        "session_id",
        "attendance.work_sessions",
        "CASCADE",
        "fk_attendance_events_session_id_work_sessions",
    ),
)
#: FK de solo id que ya cubre una compuesta (se quitan: cada una cuesta una revisión en cada inserción).
REDUNDANT = (
    ("biometrics.face_enrollments", "fk_face_enrollments_employee_id_employees", "employee_id", "workforce.employees"),
    (
        "biometrics.face_embeddings",
        "fk_face_embeddings_enrollment_id_face_enrollments",
        "enrollment_id",
        "biometrics.face_enrollments",
    ),
)

#: Filas que mezclarían empresas (cada consulta cuenta las de un caso; 0 en una base sana).
CROSS_TENANT = {
    "bitácora con empleado de otra empresa": (
        "SELECT count(*) FROM attendance.verification_logs v JOIN workforce.employees e ON e.id = v.employee_id "
        "WHERE v.company_id IS NOT NULL AND v.company_id <> e.company_id"
    ),
    "bitácora sin empresa que se pueda deducir": (
        "SELECT count(*) FROM attendance.verification_logs v LEFT JOIN auth.users u ON u.id = v.user_id "
        "WHERE v.company_id IS NULL AND v.employee_id IS NULL AND u.company_id IS NULL"
    ),
    "registros de asistencia con empleado de otra empresa": (
        "SELECT count(*) FROM attendance.attendance_events a JOIN workforce.employees e ON e.id = a.employee_id "
        "WHERE a.company_id <> e.company_id"
    ),
    "registros de asistencia con jornada de otra empresa": (
        "SELECT count(*) FROM attendance.attendance_events a JOIN attendance.work_sessions s ON s.id = a.session_id "
        "WHERE a.company_id <> s.company_id"
    ),
    "descansos con jornada de otra empresa": (
        "SELECT count(*) FROM attendance.work_breaks b JOIN attendance.work_sessions s ON s.id = b.session_id "
        "WHERE b.company_id <> s.company_id"
    ),
    "dispositivos con validador de otra empresa": (
        "SELECT count(*) FROM workforce.validator_devices d JOIN workforce.validators v ON v.id = d.validator_id "
        "WHERE d.company_id <> v.company_id"
    ),
    "pagos aplicados a cargos de otra empresa": (
        "SELECT count(*) FROM billing.payment_allocations a JOIN billing.payments p ON p.id = a.payment_id "
        "JOIN billing.charges c ON c.id = a.charge_id WHERE p.company_id <> c.company_id"
    ),
}


def _scalar(sql: str) -> int:
    return int(op.get_bind().execute(sa.text(sql)).scalar() or 0)


def _constraint_exists(table: str, name: str) -> bool:
    found = "SELECT 1 FROM pg_constraint WHERE conrelid = CAST(:table AS regclass) AND conname = :name"
    return bool(op.get_bind().execute(sa.text(found), {"table": table, "name": name}).scalar())


def _refuse_mixed_rows() -> None:
    found = {case: count for case, sql in CROSS_TENANT.items() if (count := _scalar(sql))}
    if found:
        detail = "; ".join(f"{case}: {count}" for case, count in found.items())
        raise RuntimeError(
            f"Hay filas que mezclan empresas ({detail}). No se cambió nada: revísalas y decide qué hacer con cada "
            "una antes de aplicar esta migración (ninguna se borra ni se corrige sola)."
        )


def _company_columns() -> None:
    """`company_id` copiado de la fila padre y obligatorio (la bitácora: de su empleado o de quien lo hizo)."""
    op.execute(
        "UPDATE attendance.verification_logs v SET company_id = e.company_id FROM workforce.employees e "
        "WHERE v.company_id IS NULL AND e.id = v.employee_id"
    )
    op.execute(
        "UPDATE attendance.verification_logs v SET company_id = u.company_id FROM auth.users u "
        "WHERE v.company_id IS NULL AND u.id = v.user_id AND u.company_id IS NOT NULL"
    )
    for table, column, parent in NEW_COLUMNS:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS company_id integer")
        op.execute(
            f"UPDATE {table} t SET company_id = p.company_id FROM {parent} p "  # noqa: S608 (nombres fijos)
            f"WHERE t.company_id IS NULL AND p.id = t.{column}"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN company_id SET NOT NULL")
    for table in TARGETS:
        name = f"uq_{table.split('.')[1]}_id_company"
        if not _constraint_exists(table, name):
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE (id, company_id)")


def _concurrent_index(name: str, definition: str) -> None:
    """Índice sin bloquear las escrituras; uno inválido de una falla anterior se reconstruye."""
    schema = definition.split(" ON ", 1)[1].split(".", 1)[0]
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}.{name}")
    op.execute(f"CREATE {definition.replace('INDEX ', f'INDEX CONCURRENTLY {name} ', 1)}")


def _validated_keys() -> None:
    """Fuera de la transacción: destino de las jornadas, la bitácora obligatoria y cada FK compuesta revisada sin
    frenar a nadie (`NOT VALID` toma un instante; `VALIDATE` deja leer y escribir mientras revisa)."""
    if not _constraint_exists("attendance.work_sessions", "uq_work_sessions_id_company"):
        _concurrent_index("uq_work_sessions_id_company", "UNIQUE INDEX ON attendance.work_sessions (id, company_id)")
        op.execute(
            "ALTER TABLE attendance.work_sessions ADD CONSTRAINT uq_work_sessions_id_company "
            "UNIQUE USING INDEX uq_work_sessions_id_company"
        )
    # NOT NULL sin recorrer la tabla bajo bloqueo: un CHECK validado aparte lo demuestra.
    check = "ck_verification_logs_company_required"
    if not _constraint_exists("attendance.verification_logs", check):
        op.execute(
            f"ALTER TABLE attendance.verification_logs ADD CONSTRAINT {check} CHECK (company_id IS NOT NULL) NOT VALID"
        )
    op.execute(f"ALTER TABLE attendance.verification_logs VALIDATE CONSTRAINT {check}")
    op.execute("ALTER TABLE attendance.verification_logs ALTER COLUMN company_id SET NOT NULL")
    op.execute(f"ALTER TABLE attendance.verification_logs DROP CONSTRAINT {check}")
    for key in KEYS:
        if not _constraint_exists(key.table, key.name):
            op.execute(
                f"ALTER TABLE {key.table} ADD CONSTRAINT {key.name} FOREIGN KEY ({key.column}, company_id) "
                f"REFERENCES {key.target} (id, company_id) ON DELETE {key.ondelete} NOT VALID"
            )
        op.execute(f"ALTER TABLE {key.table} VALIDATE CONSTRAINT {key.name}")
    _concurrent_index(
        "ix_face_embeddings_company_model",
        "INDEX ON biometrics.face_embeddings (company_id, model_name) INCLUDE (id, employee_id) WHERE active",
    )


def upgrade() -> None:
    _refuse_mixed_rows()
    _company_columns()
    with op.get_context().autocommit_block():
        _validated_keys()
    for key in KEYS:
        if key.old:
            op.execute(f"ALTER TABLE {key.table} DROP CONSTRAINT IF EXISTS {key.old}")
    for table, name, _, _ in REDUNDANT:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")


def downgrade() -> None:
    for table, name, column, target in REDUNDANT:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({column}) "
            f"REFERENCES {target} (id) ON DELETE CASCADE"
        )
    for key in KEYS:
        if key.old:
            op.execute(
                f"ALTER TABLE {key.table} ADD CONSTRAINT {key.old} FOREIGN KEY ({key.column}) "
                f"REFERENCES {key.target} (id) ON DELETE {key.ondelete}"
            )
        op.execute(f"ALTER TABLE {key.table} DROP CONSTRAINT IF EXISTS {key.name}")
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS biometrics.ix_face_embeddings_company_model")
    op.execute("ALTER TABLE attendance.work_sessions DROP CONSTRAINT IF EXISTS uq_work_sessions_id_company")
    for table in TARGETS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS uq_{table.split('.')[1]}_id_company")
    for table, _, _ in NEW_COLUMNS:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS company_id")
    op.execute("ALTER TABLE attendance.verification_logs ALTER COLUMN company_id DROP NOT NULL")
