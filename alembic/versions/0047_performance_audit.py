"""Auditoría de rendimiento: súper-índices medidos con volumen real y fuera los índices sin consulta

El dueño del producto pidió revisar que no haya problemas de rendimiento, simplificar la base y agregar
índices y "súper-índices". Cada cambio sale de medir con `EXPLAIN (ANALYZE, BUFFERS)` las consultas
REALES de los repositorios sobre PostgreSQL 16 con volumen (200 empresas, una de 20 000 empleados;
≈100 k empleados, 5 M de registros en la bitácora, 2 M de jornadas, 4.8 M de registros de asistencia,
1 M de métricas faciales). Las cifras de antes y después están en el README ("Índices de base de
datos").

Súper-índices (compuestos en el orden de la consulta y con INCLUDE para resolverla sin leer la tabla):

- `attendance.verification_logs` (company_id, created_at, id) INCLUDE (employee_id, success): la
  bitácora de una empresa por periodo (API de integración `since`/`until`). Antes se ordenaba por `id`
  y PostgreSQL recorría la llave primaria hacia atrás descartando millones de filas hasta llegar al
  periodo: 467 ms → 0.1 ms. El conteo con tope (filtros de empleado o resultado) sale del índice.
- `attendance.work_sessions` (company_id, work_date, scheduled_start, id) INCLUDE (status,
  employee_id), en lugar de (company_id, work_date, id): el historial en su orden exacto sin ordenar
  un día completo, y los conteos del tablero y "¿ya registró su jornada?" de un día sin leer la tabla.
- `workforce.shift_assignments` (company_id, employee_id, valid_from) INCLUDE (valid_to, shift_id):
  sustituye a (company_id) y a (employee_id, valid_from). Sirve el turno vigente de un empleado, las
  operaciones masivas (varios empleados), la FK del empleado y las asignaciones vigentes de toda la
  empresa (tablero) sin leer la tabla.
- `ops.face_attempt_metrics` (company_id, reason, created_at) en lugar de (company_id, created_at):
  "¿la empresa está bajo ataque?" se pregunta en CADA reto facial; con 20 000 intentos en la ventana
  (una empresa grande al entrar el turno) se leían todos para encontrar los sospechosos (4.9 ms); con
  el motivo antes de la fecha el índice lleva directo a ellos (0.16 ms). Un INCLUDE (reason) no bastaba:
  los intentos de la última hora aún no son "visibles para todos" y se leían de la tabla igual. Sigue
  sirviendo a la FK de la empresa.
- `workforce.employee_absences` (company_id, status, starts_on, id) en lugar de (company_id, status,
  id): la bandeja por estado en el mismo orden del listado (sin ordenar) y el contador de pendientes.
- `biometrics.face_embeddings` (employee_id, active, model_name, created_at) en lugar de (employee_id,
  active, created_at): las muestras del modelo actual de un empleado ya en orden, y "¿a quién le faltan
  muestras del modelo actual?" (en cada identificación 1:N mientras dura un cambio de motor) sin leer la
  tabla: 53 ms → 2 ms con 265 k muestras.

Parciales (`... IS NOT NULL`): las FK hacia el sitio de `work_sessions` (entrada y salida) y de
`attendance_events`. Un registro remoto o una jornada aún abierta no tiene sitio: no ocupa lugar en el
índice ni cuesta al insertar. La revisión de la FK (`site_id = $1`) implica el predicado y lo sigue
usando.

Fuera (ninguna consulta los usa y cada uno cuesta en cada inserción): los de llaves foráneas a
catálogos que nunca se borran (`status`, `check_in_mode`, `check_out_mode` de las jornadas; `action` y
`mode` de los registros; `type_code` y `status` de las ausencias; `status` de las solicitudes;
`severity` de los errores; `anti_spoofing_level` e `identify_confidence` de la política; `country_code`
de validadores y sitios; `scope` de los permisos de las llaves; la misma regla que la migración 0031),
`attendance_events` (company_id, occurred_at) (ninguna consulta filtra así) y los dos índices de
asignaciones que reemplaza el compuesto.

Se crean con `CREATE INDEX CONCURRENTLY` (sin bloquear las escrituras de tablas grandes) fuera de la
transacción de la migración. Un índice que cambia de definición se construye con otro nombre, se borra
el anterior y se renombra: la consulta nunca se queda sin índice. Repetir la migración tras una falla a
la mitad es seguro (cada paso usa IF EXISTS). Solo cambia índices: ningún dato ni tabla.

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-05 04:00:00
"""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Sufijo del nombre temporal con que se construye un índice que reemplaza a otro del mismo nombre.
_TMP = "_new"


@dataclass(frozen=True)
class Ix:
    """Definición de un índice (lo necesario para crearlo o borrarlo sin bloquear la tabla)."""

    name: str
    schema: str
    table: str
    columns: tuple[str, ...]
    include: tuple[str, ...] = ()
    where: str | None = None

    def create(self, name: str | None = None) -> None:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {self.schema}.{name or self.name}")
        op.create_index(
            name or self.name,
            self.table,
            list(self.columns),
            schema=self.schema,
            postgresql_include=list(self.include),
            postgresql_where=sa.text(self.where) if self.where else None,
            postgresql_concurrently=True,
        )

    def drop(self) -> None:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {self.schema}.{self.name}")

    def replace(self) -> None:
        """Construye este índice con un nombre temporal, borra el anterior del mismo nombre y renombra."""
        self.create(self.name + _TMP)
        self.drop()
        op.execute(f"ALTER INDEX {self.schema}.{self.name + _TMP} RENAME TO {self.name}")


#: Índices nuevos (el downgrade los borra).
NEW = (
    Ix(
        "ix_verification_logs_company_created",
        "attendance",
        "verification_logs",
        ("company_id", "created_at", "id"),
        include=("employee_id", "success"),
    ),
    Ix("ix_face_attempt_metrics_company_reason", "ops", "face_attempt_metrics", ("company_id", "reason", "created_at")),
    Ix(
        "ix_shift_assignments_company_employee",
        "workforce",
        "shift_assignments",
        ("company_id", "employee_id", "valid_from"),
        include=("valid_to", "shift_id"),
    ),
)

#: (nueva definición, definición anterior) de los índices que conservan su nombre.
REPLACED = (
    (
        Ix(
            "ix_work_sessions_company_date",
            "attendance",
            "work_sessions",
            ("company_id", "work_date", "scheduled_start", "id"),
            include=("status", "employee_id"),
        ),
        Ix("ix_work_sessions_company_date", "attendance", "work_sessions", ("company_id", "work_date", "id")),
    ),
    (
        Ix(
            "ix_work_sessions_check_in_site",
            "attendance",
            "work_sessions",
            ("check_in_site_id",),
            where="check_in_site_id IS NOT NULL",
        ),
        Ix("ix_work_sessions_check_in_site", "attendance", "work_sessions", ("check_in_site_id",)),
    ),
    (
        Ix(
            "ix_work_sessions_check_out_site",
            "attendance",
            "work_sessions",
            ("check_out_site_id",),
            where="check_out_site_id IS NOT NULL",
        ),
        Ix("ix_work_sessions_check_out_site", "attendance", "work_sessions", ("check_out_site_id",)),
    ),
    (
        Ix("ix_attendance_events_site", "attendance", "attendance_events", ("site_id",), where="site_id IS NOT NULL"),
        Ix("ix_attendance_events_site", "attendance", "attendance_events", ("site_id",)),
    ),
    (
        Ix(
            "ix_face_embeddings_employee_active",
            "biometrics",
            "face_embeddings",
            ("employee_id", "active", "model_name", "created_at"),
        ),
        Ix(
            "ix_face_embeddings_employee_active",
            "biometrics",
            "face_embeddings",
            ("employee_id", "active", "created_at"),
        ),
    ),
    (
        Ix(
            "ix_employee_absences_company_status",
            "workforce",
            "employee_absences",
            ("company_id", "status", "starts_on", "id"),
        ),
        Ix("ix_employee_absences_company_status", "workforce", "employee_absences", ("company_id", "status", "id")),
    ),
)

#: Índices que se retiran (el downgrade los vuelve a crear con su definición anterior).
DROPPED = (
    Ix("ix_work_sessions_status", "attendance", "work_sessions", ("status",)),
    Ix("ix_work_sessions_check_in_mode", "attendance", "work_sessions", ("check_in_mode",)),
    Ix("ix_work_sessions_check_out_mode", "attendance", "work_sessions", ("check_out_mode",)),
    Ix("ix_attendance_events_action", "attendance", "attendance_events", ("action",)),
    Ix("ix_attendance_events_mode", "attendance", "attendance_events", ("mode",)),
    Ix("ix_attendance_events_company_occurred", "attendance", "attendance_events", ("company_id", "occurred_at")),
    Ix("ix_employee_absences_type_code", "workforce", "employee_absences", ("type_code",)),
    Ix("ix_employee_absences_status", "workforce", "employee_absences", ("status",)),
    Ix("ix_shift_change_requests_status", "workforce", "shift_change_requests", ("status",)),
    Ix("ix_shift_assignments_employee_from", "workforce", "shift_assignments", ("employee_id", "valid_from")),
    Ix("ix_shift_assignments_company", "workforce", "shift_assignments", ("company_id",)),
    Ix("ix_face_attempt_metrics_company_created", "ops", "face_attempt_metrics", ("company_id", "created_at")),
    Ix("ix_error_reports_severity", "ops", "error_reports", ("severity",)),
    Ix("ix_verification_policy_anti_spoofing_level", "tenancy", "verification_policy", ("anti_spoofing_level",)),
    Ix("ix_verification_policy_identify_confidence", "tenancy", "verification_policy", ("identify_confidence",)),
    Ix("ix_validators_country_code", "workforce", "validators", ("country_code",)),
    Ix("ix_work_sites_country_code", "workforce", "work_sites", ("country_code",)),
    Ix("ix_company_api_key_scopes_scope", "tenancy", "company_api_key_scopes", ("scope",)),
)


def upgrade() -> None:
    # CONCURRENTLY no puede ir dentro de una transacción: cada sentencia se confirma sola.
    with op.get_context().autocommit_block():
        # Primero lo nuevo (ninguna consulta se queda sin índice) y al final lo que sobra.
        for index in NEW:
            index.create()
        for new, _ in REPLACED:
            new.replace()
        for index in DROPPED:
            index.drop()


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for index in DROPPED:
            index.create()
        for _, previous in REPLACED:
            previous.replace()
        for index in NEW:
            index.drop()
