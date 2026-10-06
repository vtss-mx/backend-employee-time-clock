"""Antifraude de identidad, fases 0 y 1: motor de riesgo explicable, casos de fraude y política auditada

Pedido del dueño del producto: "endurecer la política de verificación de identidad; todo controlado desde el ADMIN;
el sistema debe ser EVOLUTIVO". Diseño, amenazas y prototipos en `docs/rd/antifraude-identidad.md`; decisiones del
dueño del 2026-10-04 (D1-D12) en el README, "Antifraude de identidad".

Fase 0 (arreglos sin rechazos de más):
- `ops.face_attempt_metrics`: `flash_ratio` (cociente rostro/fondo del destello, P1), `verification_log_id` (enlace
  con el intento, D7) y `fraud_label` (lo que decidió la revisión de su caso). Sin CHECK en la etiqueta: validarlo
  recorrería todas las particiones con la tabla bloqueada (lo garantiza el código, que solo escribe FRAUD/GENUINE).
- `biometrics.capture_traces` (nueva, de empresa): pHash del rostro y del cuadro y el embedding CIFRADO de cada
  captura frontal, para el reenvío perceptual (P2). Su retención la acota (FACE_REPLAY_RETENTION_DAYS, depuración).
- `tenancy.verification_policy`: `qr_only_attendance` (D4: nace en `true` para las empresas que ya existían, que
  conservan su comportamiento, y queda en `false` para las nuevas), `duplicate_confidence` (nivel de SOSPECHA de
  duplicado al registrarse), `employee_device_mode` (D2, para la fase 2) y `preset`.
- `ops.policy_changes` (nueva): historial de cada cambio de la política (quién, cuándo, antes → después) y regla de
  dos personas para relajar (D12).
- `biometrics.face_enrollment_flags.details`: los empleados más parecidos de la marca POSSIBLE_DUPLICATE.

Fase 1 (motor de riesgo y casos):
- Política: corte y acción de cada nivel, acción de respaldo, ajustes por señal (JSON) y la evidencia (D1).
- `ops.risk_assessments` (nueva, particionada por mes con la retención de las métricas): la decisión de cada intento.
- `ops.fraud_cases`, `ops.fraud_case_attempts`, `ops.fraud_case_events`, `ops.fraud_evidence` (nuevas, de empresa):
  los casos que revisa el ADMIN, sus intentos, su historial y la REFERENCIA de sus fotogramas de evidencia (cifrados
  en el bucket; nunca la imagen en la BD).
- `ops.attack_signatures` (nueva, de la plataforma: solo huellas, D6) y `ops.risk_signal_stats` (línea base por
  empresa y señal).
- `biometrics.face_challenges`: `step_up` (reto de "un paso más") y `reinforced` (la empresa estaba reforzada).
- `attendance.work_sessions`: registro "en revisión" (D3) con su seguimiento; `attendance.attendance_events.under_review`.
- Catálogos nuevos (señales, niveles, acciones, modos, motivos de revisión, estados...) y sus filas en
  `verification_reasons`, `face_errors` y `enrollment_flags`; la pantalla `ADMIN_FRAUD_CASES` y el contador del menú
  de la asistencia (`PENDING_ATTENDANCE_REVIEWS`).

Tablas grandes sin frenar la operación (§3.1.13): columnas nuevas nulas o con valor constante (solo el catálogo),
índices de `work_sessions` `CONCURRENTLY` y sus llaves foráneas `NOT VALID` + `VALIDATE`. Cada tabla de empresa nueva
lleva su seguridad por fila (la misma SQL que `row_security.policy_ddl`). `alembic/sql/0062_partitions.sql` agrega
`ops.risk_assessments` a `ops.ensure_partitions` y `0062_comments.sql` comenta las tablas nuevas.

`downgrade` encola en `ops.storage_deletions` los objetos de la evidencia (lo que se borra de la BD nunca se queda en
la nube), quita todo lo nuevo y deja `ops.ensure_partitions` como en `0055`.

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-08 10:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
CATALOG = "catalog"
OPS = "ops"
TENANCY = "tenancy"
BIOMETRICS = "biometrics"
ATTENDANCE = "attendance"
AUTH = "auth"
WORKFORCE = "workforce"
EXPRESSION = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
COMPANY_COMMENT = (
    "Empresa dueña de la fila. Seguridad por fila (política tenant_isolation): solo se ve y se escribe en una "
    "transacción con app.company_id igual (o con el rol de la plataforma)."
)
#: Tablas de empresa nuevas (TENANT_TABLES de app/core/row_security.py).
TENANT_TABLES = (
    "biometrics.capture_traces",
    "ops.policy_changes",
    "ops.risk_assessments",
    "ops.fraud_cases",
    "ops.fraud_case_attempts",
    "ops.fraud_case_events",
    "ops.fraud_evidence",
    "ops.risk_signal_stats",
)
#: Catálogos simples (código, nombre, descripción, orden, activo) y los que además llevan su tono.
PLAIN_CATALOGS = (
    "fraud_kinds",
    "signal_modes",
    "review_reasons",
    "risk_actions",
    "employee_device_modes",
    "policy_presets",
    "fraud_case_event_kinds",
)
TONE_CATALOGS = ("risk_tiers", "attendance_review_statuses", "policy_change_statuses", "fraud_case_statuses")
NEW_REASONS = ("FLASH_FLAT", "REPLAY_PERCEPTUAL", "KNOWN_ATTACK", "RISK_DENIED", "STEP_UP_REQUIRED")
SCREEN = "ADMIN_FRAUD_CASES"
#: Llaves foráneas de las columnas nuevas de la política: (columna, nombre de la llave, catálogo, columna del catálogo).
POLICY_FKS = (
    (
        "duplicate_confidence",
        "fk_verification_policy_duplicate_confidence_confidence_levels",
        "confidence_levels",
        "value",
    ),
    (
        "employee_device_mode",
        "fk_verification_policy_employee_device_mode_employee_de_7b68",
        "employee_device_modes",
        "code",
    ),
    ("preset", "fk_verification_policy_preset_policy_presets", "policy_presets", "code"),
    ("risk_medium_action", "fk_verification_policy_risk_medium_action_risk_actions", "risk_actions", "code"),
    ("risk_high_action", "fk_verification_policy_risk_high_action_risk_actions", "risk_actions", "code"),
    ("risk_critical_action", "fk_verification_policy_risk_critical_action_risk_actions", "risk_actions", "code"),
    ("risk_fallback_action", "fk_verification_policy_risk_fallback_action_risk_actions", "risk_actions", "code"),
)
#: Llaves foráneas de las columnas nuevas de las jornadas (tabla grande: NOT VALID + VALIDATE).
SESSION_FKS = (
    (
        "fk_work_sessions_review_status_attendance_review_statuses",
        "FOREIGN KEY (review_status) REFERENCES catalog.attendance_review_statuses (code)",
    ),
    (
        "fk_work_sessions_reviewed_by_id_users",
        "FOREIGN KEY (reviewed_by_id) REFERENCES auth.users (id) ON DELETE SET NULL",
    ),
)
#: Índices nuevos de las jornadas (CONCURRENTLY).
SESSION_INDEXES = (
    (
        "ix_work_sessions_review_pending",
        "(company_id, work_date, scheduled_start, id) WHERE review_status = 'PENDING'",
    ),
    ("ix_work_sessions_reviewed_by_id", "(reviewed_by_id) WHERE reviewed_by_id IS NOT NULL"),
)
_JSON = postgresql.JSONB(astext_type=sa.Text())


def _run_sql(name: str) -> None:
    op.get_bind().exec_driver_sql((SQL_DIR / name).read_text(encoding="utf-8").replace("%", "%%"))


def _entry_columns() -> list[sa.Column]:
    return [
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    ]


def _catalog(name: str, rows: list[dict], *extra: sa.Column | sa.Constraint) -> None:
    table = op.create_table(
        name, *extra, *_entry_columns(), sa.PrimaryKeyConstraint("code", name=op.f(f"pk_{name}")), schema=CATALOG
    )
    op.bulk_insert(table, rows)


def _catalogs(seed: dict) -> None:
    for name in ("fraud_kinds", "signal_modes", "review_reasons"):
        _catalog(name, seed[name])
    _catalog(
        "risk_signals",
        seed["risk_signals"],
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("points", sa.SmallInteger(), nullable=False),
        sa.Column("mode", sa.String(length=30), nullable=False),
        sa.Column("hard", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("client", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("review_reason", sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(
            ["kind"], [f"{CATALOG}.fraud_kinds.code"], name=op.f("fk_risk_signals_kind_fraud_kinds")
        ),
        sa.ForeignKeyConstraint(
            ["mode"], [f"{CATALOG}.signal_modes.code"], name=op.f("fk_risk_signals_mode_signal_modes")
        ),
        sa.ForeignKeyConstraint(
            ["review_reason"],
            [f"{CATALOG}.review_reasons.code"],
            name=op.f("fk_risk_signals_review_reason_review_reasons"),
        ),
    )
    for name in PLAIN_CATALOGS[3:]:
        _catalog(name, seed[name])
    for name in TONE_CATALOGS:
        _catalog(name, seed[name], sa.Column("tone", sa.String(length=20), server_default="muted", nullable=False))


def _seed_rows(seed: dict) -> None:
    """Las filas nuevas de catálogos que ya existían (motivos, errores, marcas y la pantalla de casos). Idempotente
    (`ON CONFLICT DO NOTHING`): en una base nueva la migración inicial ya cargó el seed vigente con estas filas."""
    rows = {
        "verification_reasons": ([r for r in seed["verification_reasons"] if r["code"] in NEW_REASONS], ["code"]),
        "face_errors": ([r for r in seed["face_errors"] if r["code"] in NEW_REASONS], ["code"]),
        "enrollment_flags": ([r for r in seed["enrollment_flags"] if r["code"] == "POSSIBLE_DUPLICATE"], ["code"]),
        "screens": ([r for r in seed["screens"] if r["code"] == SCREEN], ["code"]),
        "role_screens": ([r for r in seed["role_screens"] if r["screen_code"] == SCREEN], ["role_code", "screen_code"]),
        "menu_module_screens": (
            [r for r in seed["menu_module_screens"] if r["screen_code"] == SCREEN],
            ["screen_code"],
        ),
    }
    for name, (items, keys) in rows.items():
        table = sa.table(name, *(sa.column(key) for key in items[0]), schema=CATALOG)
        op.execute(postgresql.insert(table).values(items).on_conflict_do_nothing(index_elements=keys))
    op.execute(f"UPDATE {CATALOG}.screens SET badge = 'PENDING_ATTENDANCE_REVIEWS' WHERE code = 'COMPANY_ATTENDANCE'")


def _policy() -> None:
    table = "verification_policy"
    # Las empresas que ya existían conservan el QR solo con asistencia (D4); las nuevas nacen sin ella.
    op.add_column(
        table,
        sa.Column("qr_only_attendance", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        schema=TENANCY,
    )
    op.alter_column(table, "qr_only_attendance", server_default=sa.text("false"), schema=TENANCY)
    columns = [
        sa.Column(
            "duplicate_confidence", sa.Numeric(precision=7, scale=5), server_default=sa.text("0.99"), nullable=False
        ),
        sa.Column("employee_device_mode", sa.String(length=30), server_default="OBSERVE", nullable=False),
        sa.Column("preset", sa.String(length=30), nullable=True),
        sa.Column("risk_engine", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("risk_medium_score", sa.SmallInteger(), server_default=sa.text("30"), nullable=False),
        sa.Column("risk_high_score", sa.SmallInteger(), server_default=sa.text("60"), nullable=False),
        sa.Column("risk_critical_score", sa.SmallInteger(), server_default=sa.text("80"), nullable=False),
        sa.Column("risk_medium_action", sa.String(length=30), server_default="STEP_UP", nullable=False),
        sa.Column("risk_high_action", sa.String(length=30), server_default="REVIEW", nullable=False),
        sa.Column("risk_critical_action", sa.String(length=30), server_default="DENY", nullable=False),
        sa.Column("risk_fallback_action", sa.String(length=30), server_default="ALLOW", nullable=False),
        sa.Column("risk_signals", _JSON, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("fraud_evidence", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    ]
    for column in columns:
        op.add_column(table, column, schema=TENANCY)
    # Una fila por empresa: las llaves y los CHECK se validan al crearse (instantáneo).
    for column, name, target, key in POLICY_FKS:
        op.create_foreign_key(name, table, target, [column], [key], source_schema=TENANCY, referent_schema=CATALOG)
    op.create_check_constraint(
        op.f("ck_verification_policy_risk_scores"),
        table,
        "risk_medium_score BETWEEN 1 AND 100 AND risk_high_score BETWEEN 1 AND 100 AND risk_critical_score BETWEEN 1 "
        "AND 100 AND risk_medium_score < risk_high_score AND risk_high_score < risk_critical_score",
        schema=TENANCY,
    )
    op.create_check_constraint(
        op.f("ck_verification_policy_risk_fallback_action"),
        table,
        "risk_fallback_action IN ('ALLOW', 'ALERT', 'STEP_UP')",
        schema=TENANCY,
    )


def _existing_tables() -> None:
    """Columnas nuevas de tablas que ya existían (nulas o con un valor constante: sin reescribir la tabla)."""
    metrics = "face_attempt_metrics"
    op.add_column(metrics, sa.Column("flash_ratio", sa.Float(), nullable=True), schema=OPS)
    op.add_column(metrics, sa.Column("verification_log_id", sa.BigInteger(), nullable=True), schema=OPS)
    op.add_column(metrics, sa.Column("fraud_label", sa.String(length=20), nullable=True), schema=OPS)
    for name in ("step_up", "reinforced"):
        op.add_column(
            "face_challenges",
            sa.Column(name, sa.Boolean(), server_default=sa.text("false"), nullable=False),
            schema=BIOMETRICS,
        )
    op.add_column("face_enrollment_flags", sa.Column("details", _JSON, nullable=True), schema=BIOMETRICS)
    op.add_column(
        "attendance_events",
        sa.Column("under_review", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        schema=ATTENDANCE,
    )
    sessions = "work_sessions"
    op.add_column(sessions, sa.Column("review_status", sa.String(length=20), nullable=True), schema=ATTENDANCE)
    op.add_column(sessions, sa.Column("review_reasons", sa.String(length=200), nullable=True), schema=ATTENDANCE)
    op.add_column(sessions, sa.Column("reviewed_by_id", sa.Integer(), nullable=True), schema=ATTENDANCE)
    op.add_column(sessions, sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True), schema=ATTENDANCE)
    op.add_column(sessions, sa.Column("review_note", sa.String(length=500), nullable=True), schema=ATTENDANCE)
    for name, definition in SESSION_FKS:
        op.execute(f"ALTER TABLE {ATTENDANCE}.{sessions} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {ATTENDANCE}.{sessions} ADD CONSTRAINT {name} {definition} NOT VALID")


def _validate_and_index_sessions() -> None:
    """Fuera de la transacción: validar las llaves (deja leer y escribir) e índices CONCURRENTLY (repetible)."""
    with op.get_context().autocommit_block():
        for name, _ in SESSION_FKS:
            op.execute(f"ALTER TABLE {ATTENDANCE}.work_sessions VALIDATE CONSTRAINT {name}")
        for name, definition in SESSION_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {ATTENDANCE}.{name}")
            op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {ATTENDANCE}.work_sessions {definition}")


def _users_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{AUTH}.users.id"], name=op.f(f"fk_{table}_{column}_users"), ondelete="SET NULL"
    )


def _company_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["company_id"], [f"{TENANCY}.companies.id"], name=op.f(f"fk_{table}_company_id_companies"), ondelete="CASCADE"
    )


def _catalog_fk(table: str, column: str, catalog: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{CATALOG}.{catalog}.code"], name=op.f(f"fk_{table}_{column}_{catalog}"))


def _case_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["case_id", "company_id"],
        [f"{OPS}.fraud_cases.id", f"{OPS}.fraud_cases.company_id"],
        name=f"fk_{table}_case_company",
        ondelete="CASCADE",
    )


def _traces_and_changes() -> None:
    op.create_table(
        "capture_traces",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("face_phash", sa.BigInteger(), nullable=False),
        sa.Column("frame_phash", sa.BigInteger(), nullable=False),
        sa.Column("embedding_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("dimension", sa.SmallInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["employee_id", "company_id"],
            [f"{WORKFORCE}.employees.id", f"{WORKFORCE}.employees.company_id"],
            name="fk_capture_traces_employee_company",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capture_traces")),
        schema=BIOMETRICS,
    )
    op.create_index("ix_capture_traces_created", "capture_traces", ["created_at"], schema=BIOMETRICS)
    op.create_index(
        "ix_capture_traces_employee",
        "capture_traces",
        ["company_id", "employee_id", "created_at", "id"],
        schema=BIOMETRICS,
    )
    table = "policy_changes"
    op.create_table(
        table,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("relaxes", sa.Boolean(), nullable=False),
        sa.Column("preset", sa.String(length=30), nullable=True),
        sa.Column("changes", _JSON, nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("simulation", _JSON, nullable=True),
        sa.Column("requested_by_id", sa.Integer(), nullable=True),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(length=500), nullable=True),
        _company_fk(table),
        _catalog_fk(table, "status", "policy_change_statuses"),
        _catalog_fk(table, "preset", "policy_presets"),
        _users_fk(table, "requested_by_id"),
        _users_fk(table, "decided_by_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_changes")),
        schema=OPS,
    )
    op.create_index("ix_policy_changes_company", table, ["company_id", "created_at", "id"], schema=OPS)


def _assessments() -> None:
    table = "risk_assessments"
    op.create_table(
        table,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("verification_log_id", sa.BigInteger(), nullable=True),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("reasons", _JSON, nullable=False),
        sa.Column("policy_version", sa.String(length=16), nullable=False),
        sa.Column("engine", sa.String(length=10), nullable=False),
        sa.Column("step_up", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("fallback", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("fraud_label", sa.String(length=20), nullable=True),
        sa.CheckConstraint("fraud_label IN ('FRAUD', 'GENUINE')", name=op.f("ck_risk_assessments_fraud_label")),
        _company_fk(table),
        _catalog_fk(table, "tier", "risk_tiers"),
        _catalog_fk(table, "action", "risk_actions"),
        sa.PrimaryKeyConstraint("id", "created_at", name=op.f("pk_risk_assessments")),
        schema=OPS,
        postgresql_partition_by="RANGE (created_at)",
    )
    op.execute(f"CREATE TABLE {OPS}.{table}_default PARTITION OF {OPS}.{table} DEFAULT")
    op.create_index("ix_risk_assessments_company", table, ["company_id", "created_at", "id"], schema=OPS)


def _cases() -> None:
    table = "fraud_cases"
    active = sa.text("status IN ('OPEN', 'IN_REVIEW')")
    op.create_table(
        table,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("subject", sa.String(length=40), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_score", sa.SmallInteger(), nullable=True),
        sa.Column("tier", sa.String(length=20), nullable=True),
        sa.Column("reason", sa.String(length=50), nullable=False),
        sa.Column("evidence", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(length=1000), nullable=True),
        sa.UniqueConstraint("id", "company_id", name="uq_fraud_cases_id_company"),
        sa.ForeignKeyConstraint(
            ["employee_id", "company_id"],
            [f"{WORKFORCE}.employees.id", f"{WORKFORCE}.employees.company_id"],
            name="fk_fraud_cases_employee_company",
            ondelete="CASCADE",
        ),
        _company_fk(table),
        _catalog_fk(table, "status", "fraud_case_statuses"),
        _catalog_fk(table, "kind", "fraud_kinds"),
        _users_fk(table, "actor_id"),
        _catalog_fk(table, "tier", "risk_tiers"),
        _users_fk(table, "decided_by_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fraud_cases")),
        schema=OPS,
    )
    op.create_index(
        "uq_fraud_cases_active_subject",
        table,
        ["company_id", "subject"],
        unique=True,
        postgresql_where=active,
        schema=OPS,
    )
    op.create_index("ix_fraud_cases_queue", table, ["status", "last_attempt_at", "id"], schema=OPS)
    op.create_index("ix_fraud_cases_recent", table, ["last_attempt_at", "id"], schema=OPS)
    op.create_index("ix_fraud_cases_company", table, ["company_id", "last_attempt_at", "id"], schema=OPS)
    _case_children()


def _case_children() -> None:
    big = sa.BigInteger()
    op.create_table(
        "fraud_case_attempts",
        sa.Column("id", big, autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("case_id", big, nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verification_log_id", big, nullable=True),
        sa.Column("assessment_id", big, nullable=True),
        sa.Column("metric_id", big, nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=50), nullable=True),
        sa.Column("score", sa.SmallInteger(), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=True),
        sa.Column("signals", _JSON, nullable=False),
        sa.Column("metrics", _JSON, nullable=False),
        sa.Column("phashes", _JSON, nullable=False),
        sa.Column("camera", sa.String(length=120), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        _case_fk("fraud_case_attempts"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fraud_case_attempts")),
        schema=OPS,
    )
    op.create_index("ix_fraud_case_attempts_case", "fraud_case_attempts", ["case_id", "id"], schema=OPS)
    op.create_table(
        "fraud_case_events",
        sa.Column("id", big, autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("case_id", big, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=True),
        sa.Column("status_from", sa.String(length=20), nullable=True),
        sa.Column("status_to", sa.String(length=20), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        _case_fk("fraud_case_events"),
        _catalog_fk("fraud_case_events", "kind", "fraud_case_event_kinds"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fraud_case_events")),
        schema=OPS,
    )
    op.create_index("ix_fraud_case_events_case", "fraud_case_events", ["case_id", "id"], schema=OPS)
    op.create_table(
        "fraud_evidence",
        sa.Column("id", big, autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("case_id", big, nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=True),
        sa.Column("verification_log_id", big, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("uid", sa.String(length=32), nullable=False),
        sa.Column("content_type", sa.String(length=30), nullable=True),
        sa.Column("object_name", sa.String(length=300), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        _case_fk("fraud_evidence"),
        sa.CheckConstraint("kind IN ('FRONTAL', 'STEP', 'FLASH')", name=op.f("ck_fraud_evidence_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fraud_evidence")),
        schema=OPS,
    )
    op.create_index("ix_fraud_evidence_case", "fraud_evidence", ["case_id", "id"], schema=OPS)
    op.create_index("ix_fraud_evidence_created", "fraud_evidence", ["created_at"], schema=OPS)


def _intelligence() -> None:
    table = "attack_signatures"
    op.create_table(
        table,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("case_id", sa.BigInteger(), nullable=True),
        sa.Column("companies", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("hits", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("allowed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "kind", "value", "company_id", name="uq_attack_signatures_kind", postgresql_nulls_not_distinct=True
        ),
        sa.CheckConstraint("kind IN ('CAPTURE_PHASH')", name=op.f("ck_attack_signatures_kind")),
        _company_fk(table),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attack_signatures")),
        schema=OPS,
    )
    op.create_index("ix_attack_signatures_expires", table, ["expires_at"], schema=OPS)
    table = "risk_signal_stats"
    op.create_table(
        table,
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("signal", sa.String(length=30), nullable=False),
        sa.Column("confirmed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("false_positive", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _company_fk(table),
        sa.ForeignKeyConstraint(
            ["signal"], [f"{CATALOG}.risk_signals.code"], name=op.f("fk_risk_signal_stats_signal_risk_signals")
        ),
        sa.PrimaryKeyConstraint("company_id", "signal", name=op.f("pk_risk_signal_stats")),
        schema=OPS,
    )


def _row_security() -> None:
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({EXPRESSION}) WITH CHECK ({EXPRESSION})")
        op.execute(f"COMMENT ON COLUMN {table}.company_id IS '{COMPANY_COMMENT}'")


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    _catalogs(seed)
    _seed_rows(seed)
    _policy()
    _existing_tables()
    _traces_and_changes()
    _assessments()
    _cases()
    _intelligence()
    _row_security()
    _run_sql("0062_partitions.sql")
    op.get_bind().execute(sa.text("SELECT ops.ensure_partitions('ops.risk_assessments', CAST(now() AS date), 3, NULL)"))
    _run_sql("0062_comments.sql")
    _validate_and_index_sessions()


def downgrade() -> None:
    # Lo que se borra de la BD nunca se queda en la nube: los objetos de la evidencia pasan a la cola de borrado.
    op.execute(
        f"INSERT INTO {OPS}.storage_deletions (object_name, requested_at) "
        f"SELECT object_name, now() FROM {OPS}.fraud_evidence WHERE object_name IS NOT NULL "
        "ON CONFLICT (object_name) DO NOTHING"
    )
    with op.get_context().autocommit_block():
        for name, _ in SESSION_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {ATTENDANCE}.{name}")
    for name in (
        "risk_signal_stats",
        "attack_signatures",
        "fraud_evidence",
        "fraud_case_events",
        "fraud_case_attempts",
    ):
        op.drop_table(name, schema=OPS)
    op.drop_table("fraud_cases", schema=OPS)
    op.drop_table("risk_assessments", schema=OPS)  # con sus particiones
    op.drop_table("policy_changes", schema=OPS)
    op.drop_table("capture_traces", schema=BIOMETRICS)
    _run_sql("0055_partitions.sql")
    for name, _ in SESSION_FKS:
        op.execute(f"ALTER TABLE {ATTENDANCE}.work_sessions DROP CONSTRAINT IF EXISTS {name}")
    for column in ("review_note", "reviewed_at", "reviewed_by_id", "review_reasons", "review_status"):
        op.drop_column("work_sessions", column, schema=ATTENDANCE)
    op.drop_column("attendance_events", "under_review", schema=ATTENDANCE)
    op.drop_column("face_enrollment_flags", "details", schema=BIOMETRICS)
    op.drop_column("face_challenges", "reinforced", schema=BIOMETRICS)
    op.drop_column("face_challenges", "step_up", schema=BIOMETRICS)
    for column in ("fraud_label", "verification_log_id", "flash_ratio"):
        op.drop_column("face_attempt_metrics", column, schema=OPS)
    for name in ("ck_verification_policy_risk_fallback_action", "ck_verification_policy_risk_scores"):
        op.drop_constraint(name, "verification_policy", schema=TENANCY, type_="check")
    for column, name, _, _ in reversed(POLICY_FKS):
        op.drop_constraint(name, "verification_policy", schema=TENANCY, type_="foreignkey")
    for column in (
        "fraud_evidence",
        "risk_signals",
        "risk_fallback_action",
        "risk_critical_action",
        "risk_high_action",
        "risk_medium_action",
        "risk_critical_score",
        "risk_high_score",
        "risk_medium_score",
        "risk_engine",
        "preset",
        "employee_device_mode",
        "duplicate_confidence",
        "qr_only_attendance",
    ):
        op.drop_column("verification_policy", column, schema=TENANCY)
    op.execute(f"UPDATE {CATALOG}.screens SET badge = NULL WHERE code = 'COMPANY_ATTENDANCE'")
    op.execute(f"DELETE FROM {CATALOG}.menu_module_screens WHERE screen_code = '{SCREEN}'")
    op.execute(f"DELETE FROM {CATALOG}.role_screens WHERE screen_code = '{SCREEN}'")
    op.execute(f"DELETE FROM {CATALOG}.screens WHERE code = '{SCREEN}'")
    op.execute(f"DELETE FROM {CATALOG}.enrollment_flags WHERE code = 'POSSIBLE_DUPLICATE'")
    codes = ", ".join(f"'{code}'" for code in NEW_REASONS)
    op.execute(f"DELETE FROM {CATALOG}.face_errors WHERE code IN ({codes})")
    op.execute(f"DELETE FROM {CATALOG}.verification_reasons WHERE code IN ({codes})")
    for name in (
        *reversed(TONE_CATALOGS),
        *reversed(PLAIN_CATALOGS[3:]),
        "risk_signals",
        *reversed(PLAIN_CATALOGS[:3]),
    ):
        op.drop_table(name, schema=CATALOG)
