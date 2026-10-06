"""Tablas que crecen sin límite particionadas por mes, y espacio libre en las páginas de las que se actualizan seguido

Regla 15 del `AGENTS.md` raíz (millones de operaciones por minuto): una bitácora de cientos de millones de filas en
UNA tabla tiene índices cada vez más profundos, un autovacuum que la recorre completa y una depuración fila por fila.
Particionadas por mes (`app/core/partitions.py`), cada consulta por periodo lee solo sus meses, cada mes tiene
índices chicos y lo que vence sale con `DROP TABLE` de su partición:

| Tabla | Llave de partición | Retención |
|---|---|---|
| `attendance.verification_logs` | `created_at` | se conserva |
| `attendance.attendance_events` | `occurred_at` | se conserva |
| `ops.face_attempt_metrics` | `created_at` | FACE_METRICS_RETENTION_DAYS |
| `ops.error_occurrences` | `occurred_at` | ERROR_OCCURRENCE_RETENTION_DAYS |
| `ops.usage_routes`, `ops.usage_users` | `day` | USAGE_DETAIL_RETENTION_DAYS |
| `ops.storage_snapshots` | `day` | USAGE_RETENTION_DAYS |

No se particionan (y por qué): `work_sessions` (la base garantiza UNA jornada abierta por empleado con un índice
único parcial, que en una tabla particionada tendría que incluir la fecha y dejaría de garantizarlo),
`capture_fingerprints` (la huella debe ser única en toda la plataforma para detectar un reenvío), `auth_sessions`,
`employee_qr_codes`, `face_challenges` (su vencimiento es por fila y su llave no es de tiempo) y `usage_daily`,
`headcount_days` (una fila por empresa y día: su retención ya las acota).

**Sin copiar los datos**: la tabla actual se convierte en la partición `<tabla>_legacy` (todo lo anterior al mes
siguiente al de la migración) de la tabla particionada nueva con el mismo nombre; los meses siguientes son
particiones nuevas, más una `_default` para que ninguna fila se pierda si el mantenimiento no corre. Lo que
cambia de definición se prepara antes, sin bloquear (`CONCURRENTLY`; la llave primaria pasa a `(id, fecha)`, como
exige PostgreSQL), y un CHECK validado aparte demuestra el rango de `_legacy`: el cambio de nombre y el enganche
duran un instante aunque la tabla tenga millones de filas (medido en el README). Una tabla vacía no conserva
`_legacy`. La FK de `attendance_events.verification_log_id` se quita (una FK hacia una tabla particionada exigiría
copiar su fecha; ambas filas se escriben en la misma transacción). `ix_verification_logs_user_created` suma
`company_id` al INCLUDE (con la seguridad por fila de `0056` el conteo sigue sin leer la tabla) y
`ix_attendance_events_employee` pasa a `(employee_id, occurred_at, id)` (el último registro con ubicación lee solo
los meses en que un viaje aún podría ser imposible) e `ix_attendance_events_site` a `(company_id, site_id)` (la FK
compuesta y "¿ya se checó en el sitio?" sin leer la tabla, también con la seguridad por fila).

`ops.ensure_partitions` (`alembic/sql/0055_partitions.sql`) crea y borra particiones: SECURITY DEFINER del dueño
y solo para estas tablas, porque la API no tiene permisos de DDL; la ejecuta el mantenimiento en cada vuelta.

Espacio libre (`fillfactor`) en las tablas que se actualizan seguido sin cambiar columnas de sus índices: así el
cambio cabe en la misma página (actualización HOT, sin tocar índices). `auth_sessions` 90 (cada renovación),
`rate_limit_counters` 70 (cada petición limitada), `usage_daily` y las particiones de `usage_routes`/`usage_users` 80
(cada lote del medidor suma), `validator_devices` y `company_api_keys` 90 (último uso).

`downgrade` deja las tablas como antes COPIANDO las filas de vuelta a una tabla normal (bajar de versión es raro;
subir no copia nada).

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-05 22:30:00
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "0055_partitions.sql"
#: Meses que se dejan creados por adelantado (después lo hace el mantenimiento: PARTITION_MONTHS_AHEAD).
MONTHS_AHEAD = 3


@dataclass(frozen=True)
class Spec:
    """Una tabla a particionar: llave, llave primaria final y anterior, índices, FKs y su secuencia."""

    table: str
    key: str
    pk: tuple[str, ...]
    old_pk: tuple[str, ...]
    #: (nombre, definición sin "CREATE INDEX nombre ON tabla") de la tabla particionada.
    indexes: tuple[tuple[str, str], ...]
    #: Índices de la tabla actual con otra definición (se reconstruyen) o que desaparecen.
    changed: tuple[str, ...] = ()
    dropped: tuple[tuple[str, str], ...] = ()
    #: (nombre, definición) de las FK de la tabla particionada (las mismas de la tabla actual).
    fks: tuple[tuple[str, str], ...] = ()
    sequence: str | None = None

    @property
    def schema(self) -> str:
        return self.table.split(".")[0]

    @property
    def name(self) -> str:
        return self.table.split(".")[1]

    @property
    def legacy(self) -> str:
        return f"{self.table}_legacy"

    @property
    def bound_check(self) -> str:
        return f"ck_{self.name}_legacy_bound"


_COMPANY = "FOREIGN KEY (company_id) REFERENCES tenancy.companies (id) ON DELETE CASCADE"
_EMPLOYEE = "FOREIGN KEY (employee_id, company_id) REFERENCES workforce.employees (id, company_id) ON DELETE CASCADE"
SPECS = (
    Spec(
        "attendance.verification_logs",
        "created_at",
        ("id", "created_at"),
        ("id",),
        (
            ("ix_verification_logs_employee_created", "(employee_id, created_at, id)"),
            ("ix_verification_logs_company_log", "(company_id, id)"),
            ("ix_verification_logs_company_created", "(company_id, created_at, id) INCLUDE (employee_id, success)"),
            (
                "ix_verification_logs_user_created",
                "(user_id, created_at, id) INCLUDE (success, method, company_id) WHERE user_id IS NOT NULL",
            ),
        ),
        changed=("ix_verification_logs_user_created",),
        fks=(
            ("fk_verification_logs_company_id_companies", _COMPANY),
            (
                "fk_verification_logs_employee_company",
                _EMPLOYEE,
            ),
            (
                "fk_verification_logs_method_verification_methods",
                "FOREIGN KEY (method) REFERENCES catalog.verification_methods (code)",
            ),
            (
                "fk_verification_logs_reason_verification_reasons",
                "FOREIGN KEY (reason) REFERENCES catalog.verification_reasons (code)",
            ),
            (
                "fk_verification_logs_user_id_users",
                "FOREIGN KEY (user_id) REFERENCES auth.users (id) ON DELETE SET NULL",
            ),
        ),
        sequence="attendance.verification_logs_id_seq",
    ),
    Spec(
        "attendance.attendance_events",
        "occurred_at",
        ("id", "occurred_at"),
        ("id",),
        (
            ("ix_attendance_events_actor_id", "(actor_id) WHERE actor_id IS NOT NULL"),
            ("ix_attendance_events_employee", "(employee_id, occurred_at, id)"),
            ("ix_attendance_events_session", "(session_id, id)"),
            ("ix_attendance_events_site", "(company_id, site_id) WHERE site_id IS NOT NULL"),
        ),
        changed=("ix_attendance_events_employee", "ix_attendance_events_site"),
        dropped=(
            (
                "ix_attendance_events_verification_log_id",
                "(verification_log_id) WHERE verification_log_id IS NOT NULL",
            ),
        ),
        fks=(
            (
                "fk_attendance_events_action_attendance_actions",
                "FOREIGN KEY (action) REFERENCES catalog.attendance_actions (code)",
            ),
            (
                "fk_attendance_events_actor_id_users",
                "FOREIGN KEY (actor_id) REFERENCES auth.users (id) ON DELETE SET NULL",
            ),
            (
                "fk_attendance_events_employee_company",
                _EMPLOYEE,
            ),
            ("fk_attendance_events_mode_work_modes", "FOREIGN KEY (mode) REFERENCES catalog.work_modes (code)"),
            (
                "fk_attendance_events_session_company",
                "FOREIGN KEY (session_id, company_id) REFERENCES attendance.work_sessions (id, company_id) "
                "ON DELETE CASCADE",
            ),
            (
                "fk_attendance_events_site_company",
                "FOREIGN KEY (site_id, company_id) REFERENCES workforce.work_sites (id, company_id) ON DELETE RESTRICT",
            ),
        ),
        sequence="attendance.attendance_events_id_seq",
    ),
    Spec(
        "ops.face_attempt_metrics",
        "created_at",
        ("id", "created_at"),
        ("id",),
        (
            ("ix_face_attempt_metrics_company_reason", "(company_id, reason, created_at)"),
            ("ix_face_attempt_metrics_created", "(created_at, id)"),
        ),
        fks=(("fk_face_attempt_metrics_company_id_companies", _COMPANY),),
        sequence="ops.face_attempt_metrics_id_seq",
    ),
    Spec(
        "ops.error_occurrences",
        "occurred_at",
        ("id", "occurred_at"),
        ("id",),
        (
            ("ix_error_occurrences_report", "(report_id, id)"),
            ("ix_error_occurrences_occurred_at", "(occurred_at)"),
        ),
        fks=(
            (
                "fk_error_occurrences_report_id_error_reports",
                "FOREIGN KEY (report_id) REFERENCES ops.error_reports (id) ON DELETE CASCADE",
            ),
        ),
        sequence="ops.error_occurrences_id_seq",
    ),
    Spec(
        "ops.usage_routes",
        "day",
        ("company_id", "day", "route"),
        ("company_id", "day", "route"),
        (("ix_usage_routes_day", "(day)"),),
    ),
    Spec(
        "ops.usage_users",
        "day",
        ("company_id", "day", "user_id"),
        ("company_id", "day", "user_id"),
        (("ix_usage_users_day", "(day)"),),
    ),
    Spec(
        "ops.storage_snapshots",
        "day",
        ("company_id", "day", "category"),
        ("company_id", "day", "category"),
        (("ix_storage_snapshots_day", "(day)"),),
        fks=(
            (
                "fk_storage_snapshots_category_storage_categories",
                "FOREIGN KEY (category) REFERENCES catalog.storage_categories (code)",
            ),
        ),
    ),
)
#: La referencia de cada registro de asistencia a su intento en la bitácora (sin FK desde esta versión).
LOG_FK = "fk_attendance_events_verification_log_id_verification_logs"
#: Espacio libre por página (%) de las tablas normales que se actualizan seguido (las particiones de consumo las
#: crea la función con el suyo).
FILLFACTOR = {
    "auth.auth_sessions": 90,
    "auth.rate_limit_counters": 70,
    "ops.usage_daily": 80,
    "workforce.validator_devices": 90,
    "tenancy.company_api_keys": 90,
}
#: El primer día del mes subsiguiente (UTC): `_legacy` cubre hasta ahí; una fila escrita mientras la migración
#: corre (aunque cambie el mes) cabe en su CHECK.
LEGACY_UNTIL = "(date_trunc('month', now() AT TIME ZONE 'UTC') + interval '2 months')::date"


def _run_sql(sql: str) -> None:
    """SQL de varias sentencias tal cual (psycopg toma `%` como marcador de parámetro: se escribe doble)."""
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def _legacy_until() -> str:
    return str(op.get_bind().execute(sa.text(f"SELECT {LEGACY_UNTIL}")).scalar())


def _constraint_exists(table: str, name: str) -> bool:
    found = "SELECT 1 FROM pg_constraint WHERE conrelid = CAST(:table AS regclass) AND conname = :name"
    return bool(op.get_bind().execute(sa.text(found), {"table": table, "name": name}).scalar())


def _prepare(spec: Spec, until: str) -> None:
    """Sin bloquear (fuera de la transacción): la llave primaria nueva y los índices que cambian, construidos con
    nombre temporal sobre la tabla actual, y el CHECK que demuestra que todo cabe en `_legacy`."""
    schema = spec.schema
    jobs = (
        [(f"pk_{spec.name}_new", f"UNIQUE INDEX ON {spec.table} ({', '.join(spec.pk)})")]
        if spec.pk != spec.old_pk
        else []
    )
    definitions = dict(spec.indexes)
    jobs += [(f"{name}_new", f"INDEX ON {spec.table} {definitions[name]}") for name in spec.changed]
    for name, definition in jobs:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}.{name}")
        op.execute(f"CREATE {definition.replace('INDEX ', f'INDEX CONCURRENTLY {name} ', 1)}")
    if not _constraint_exists(spec.table, spec.bound_check):
        op.execute(
            f"ALTER TABLE {spec.table} ADD CONSTRAINT {spec.bound_check} "
            f"CHECK ({spec.key} IS NOT NULL AND {spec.key} < '{until}') NOT VALID"
        )
    op.execute(f"ALTER TABLE {spec.table} VALIDATE CONSTRAINT {spec.bound_check}")


_RENAME_INDEXES = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
           WHERE i.indrelid = CAST(:legacy AS regclass) AND c.relname NOT LIKE '%_legacy'
  LOOP
    EXECUTE format('ALTER INDEX %I.%I RENAME TO %I', :schema, r.relname,
                   left(regexp_replace(r.relname, '_new$', ''), 56) || '_legacy');
  END LOOP;
END $$
"""


def _swap(spec: Spec, until: str) -> None:
    """Dentro de la transacción: la tabla actual pasa a `_legacy` y queda enganchada a la particionada nueva."""
    schema, legacy_name = spec.schema, f"{spec.name}_legacy"
    op.execute(f"ALTER TABLE {spec.table} RENAME TO {legacy_name}")
    for name in spec.changed:
        op.execute(f"DROP INDEX {schema}.{name}")
    for name, _ in spec.dropped:
        op.execute(f"DROP INDEX IF EXISTS {schema}.{name}")
    if spec.pk != spec.old_pk:
        op.execute(f"ALTER TABLE {spec.legacy} DROP CONSTRAINT pk_{spec.name}")
        op.execute(
            f"ALTER TABLE {spec.legacy} ADD CONSTRAINT pk_{legacy_name} PRIMARY KEY USING INDEX pk_{spec.name}_new"
        )
    rename = _RENAME_INDEXES.replace(":legacy", f"'{spec.legacy}'").replace(":schema", f"'{schema}'")
    _run_sql(rename)
    op.execute(
        f"CREATE TABLE {spec.table} (LIKE {spec.legacy} INCLUDING DEFAULTS INCLUDING CONSTRAINTS) "
        f"PARTITION BY RANGE ({spec.key})"
    )
    op.execute(f"ALTER TABLE {spec.table} DROP CONSTRAINT {spec.bound_check}")
    op.execute(f"ALTER TABLE {spec.table} ADD CONSTRAINT pk_{spec.name} PRIMARY KEY ({', '.join(spec.pk)})")
    for name, definition in spec.indexes:
        op.execute(f"CREATE INDEX {name} ON {spec.table} {definition}")
    for name, definition in spec.fks:
        op.execute(f"ALTER TABLE {spec.table} ADD CONSTRAINT {name} {definition}")
    if spec.sequence:
        op.execute(f"ALTER SEQUENCE {spec.sequence} OWNED BY {spec.table}.id")
    # Enganche sin recorrer la tabla (el CHECK validado demuestra el rango); índices, llave primaria y FK
    # equivalentes de `_legacy` se reutilizan (no se reconstruye ni se revalida nada).
    op.execute(f"ALTER TABLE {spec.table} ATTACH PARTITION {spec.legacy} FOR VALUES FROM (MINVALUE) TO ('{until}')")
    op.execute(f"ALTER TABLE {spec.legacy} DROP CONSTRAINT {spec.bound_check}")
    empty = f"SELECT NOT EXISTS (SELECT 1 FROM {spec.legacy})"  # noqa: S608 (nombres fijos de esta migración)
    if op.get_bind().execute(sa.text(empty)).scalar():
        op.execute(f"DROP TABLE {spec.legacy}")
    op.execute(f"CREATE TABLE {spec.table}_default PARTITION OF {spec.table} DEFAULT")
    op.get_bind().execute(
        sa.text("SELECT ops.ensure_partitions(:table, CAST(now() AS date), :ahead, NULL)"),
        {"table": spec.table, "ahead": MONTHS_AHEAD},
    )


def upgrade() -> None:
    _run_sql(SQL_FILE.read_text(encoding="utf-8"))
    until = _legacy_until()
    with op.get_context().autocommit_block():
        for spec in SPECS:
            _prepare(spec, until)
    op.execute(f"ALTER TABLE attendance.attendance_events DROP CONSTRAINT IF EXISTS {LOG_FK}")
    for spec in SPECS:
        _swap(spec, until)
    for table, percent in FILLFACTOR.items():
        op.execute(f"ALTER TABLE {table} SET (fillfactor = {percent})")
    for table in ("ops.usage_routes", "ops.usage_users"):
        if op.get_bind().execute(sa.text("SELECT to_regclass(:t)"), {"t": f"{table}_legacy"}).scalar():
            op.execute(f"ALTER TABLE {table}_legacy SET (fillfactor = 80)")


def _unpartition(spec: Spec) -> None:
    """La tabla vuelve a ser una tabla normal con sus filas (copiadas de todas sus particiones)."""
    old = f"{spec.name}_unpartitioned"
    op.execute(f"CREATE TABLE {spec.schema}.{old} (LIKE {spec.table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
    op.execute(f"INSERT INTO {spec.schema}.{old} SELECT * FROM {spec.table}")  # noqa: S608 (nombres fijos)
    if spec.sequence:
        op.execute(f"ALTER SEQUENCE {spec.sequence} OWNED BY {spec.schema}.{old}.id")
    op.execute(f"DROP TABLE {spec.table}")
    op.execute(f"ALTER TABLE {spec.schema}.{old} RENAME TO {spec.name}")
    op.execute(f"ALTER TABLE {spec.table} ADD CONSTRAINT pk_{spec.name} PRIMARY KEY ({', '.join(spec.old_pk)})")
    old_definitions = dict(spec.indexes)
    for name in spec.changed:
        old_definitions[name] = {
            "ix_verification_logs_user_created": "(user_id, created_at, id) INCLUDE (success, method) "
            "WHERE user_id IS NOT NULL",
            "ix_attendance_events_employee": "(employee_id, id)",
            "ix_attendance_events_site": "(site_id) WHERE site_id IS NOT NULL",
        }[name]
    for name, definition in [*old_definitions.items(), *spec.dropped]:
        op.execute(f"CREATE INDEX {name} ON {spec.table} {definition}")
    for name, definition in spec.fks:
        op.execute(f"ALTER TABLE {spec.table} ADD CONSTRAINT {name} {definition}")


def downgrade() -> None:
    for table in FILLFACTOR:
        op.execute(f"ALTER TABLE {table} RESET (fillfactor)")
    for spec in SPECS:
        _unpartition(spec)
    op.execute(
        f"ALTER TABLE attendance.attendance_events ADD CONSTRAINT {LOG_FK} FOREIGN KEY (verification_log_id) "
        "REFERENCES attendance.verification_logs (id) ON DELETE SET NULL"
    )
    op.execute("DROP FUNCTION IF EXISTS ops.ensure_partitions(text, date, integer, date)")
