"""Turnos de trabajo y asistencia por turno (entrada, descansos y salida con rostro y ubicación)

- `workforce.work_sites`: sitios de trabajo con su geocerca (domicilio, punto en el mapa y radio).
- `workforce.shifts`: turnos (entrada, salida, días, descansos y tolerancias; nocturnos incluidos).
- `workforce.shift_assignments` (+ `shift_assignment_sites`): el turno de cada empleado desde una
  fecha, sus días remotos y sus sitios. Un cambio de turno es una asignación nueva: lo ya registrado
  conserva su turno.
- `workforce.shift_change_requests`: solicitudes de cambio de turno del empleado.
- `attendance.work_sessions`, `attendance.work_breaks`, `attendance.attendance_events`: la jornada
  de cada turno, sus descansos y la bitácora (de solo inserción) de cada registro con su evidencia.
- Catálogos: `work_modes`, `attendance_actions`, `work_session_statuses`, `shift_request_statuses`,
  `board_states` (en qué va cada empleado en el tablero) y `assignment_states` (vigencia de una
  asignación): nombres y colores en la BD, no en la app.
- Pantallas `COMPANY_ATTENDANCE`, `COMPANY_SHIFTS`, `COMPANY_SITES` y `EMPLOYEE_ATTENDANCE` con sus
  permisos; el menú se renumera y las descripciones quedan como en el seed (la de Reportes aún
  hablaba del asistente que se quitó en 0040).
- Política de verificación: precisión mínima de la ubicación, detección de viaje imposible y velocidad
  máxima creíble (registros de asistencia).
- Corrige el nombre del CHECK de `min_capture_quality` (0041 lo creó sin `op.f`, y la convención de
  nombres le duplicó el prefijo): se renombra para que la base coincida con los modelos.

No modifica datos existentes.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-04 20:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
CATALOGS = (
    "work_modes",
    "attendance_actions",
    "work_session_statuses",
    "shift_request_statuses",
    "board_states",
    "assignment_states",
)
SCREENS = ("COMPANY_ATTENDANCE", "COMPANY_SHIFTS", "COMPANY_SITES", "EMPLOYEE_ATTENDANCE")
POLICY_CHECKS = (
    ("ck_verification_policy_max_location_accuracy_m", "max_location_accuracy_m BETWEEN 10 AND 1000"),
    ("ck_verification_policy_max_travel_kmh", "max_travel_kmh BETWEEN 30 AND 1000"),
)
#: Nombre con el que 0041 creó el CHECK de la calidad mínima (prefijo duplicado y truncado a 63).
QUALITY_CHECK_0041 = "ck_verification_policy_ck_verification_policy_min_captu_2465"
QUALITY_CHECK = "ck_verification_policy_min_capture_quality"
TABLES = (
    ("attendance", "work_breaks"),
    ("attendance", "attendance_events"),
    ("workforce", "shift_assignment_sites"),
    ("attendance", "work_sessions"),
    ("workforce", "shift_change_requests"),
    ("workforce", "shift_assignments"),
    ("workforce", "work_sites"),
    ("workforce", "shifts"),
    ("catalog", "board_states"),
    ("catalog", "assignment_states"),
    ("catalog", "work_session_statuses"),
    ("catalog", "work_modes"),
    ("catalog", "shift_request_statuses"),
    ("catalog", "attendance_actions"),
)


def _status_catalog(name: str) -> None:
    """Catálogo con tono (como work_session_statuses): código, nombre, descripción y color."""
    op.create_table(
        name,
        sa.Column("tone", sa.String(length=20), server_default="muted", nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f(f"pk_{name}")),
        schema="catalog",
    )


def _tables() -> None:
    _status_catalog("board_states")
    _status_catalog("assignment_states")
    op.create_table(
        "attendance_actions",
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_attendance_actions")),
        schema="catalog",
    )
    op.create_table(
        "shift_request_statuses",
        sa.Column("tone", sa.String(length=20), server_default="muted", nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_shift_request_statuses")),
        schema="catalog",
    )
    op.create_table(
        "work_modes",
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_work_modes")),
        schema="catalog",
    )
    op.create_table(
        "work_session_statuses",
        sa.Column("tone", sa.String(length=20), server_default="muted", nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_work_session_statuses")),
        schema="catalog",
    )
    op.create_table(
        "shifts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("weekdays", sa.SmallInteger(), nullable=False),
        sa.Column("breaks_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("break_minutes", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("early_check_in_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("late_tolerance_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("early_check_out_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("late_check_out_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "breaks_count BETWEEN 0 AND 6 AND (breaks_count = 0 OR break_minutes BETWEEN 5 AND 240)",
            name=op.f("ck_shifts_breaks"),
        ),
        sa.CheckConstraint(
            "early_check_in_minutes BETWEEN 0 AND 240 AND late_tolerance_minutes BETWEEN 0 AND 240 AND early_check_out_minutes BETWEEN 0 AND 240 AND late_check_out_minutes BETWEEN 0 AND 720",
            name=op.f("ck_shifts_tolerances"),
        ),
        sa.CheckConstraint("start_time <> end_time", name=op.f("ck_shifts_times")),
        sa.CheckConstraint("weekdays BETWEEN 1 AND 127", name=op.f("ck_shifts_weekdays")),
        sa.ForeignKeyConstraint(
            ["company_id"], ["tenancy.companies.id"], name=op.f("fk_shifts_company_id_companies"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shifts")),
        sa.UniqueConstraint("id", "company_id", name="uq_shifts_id_company"),
        schema="workforce",
    )
    with op.batch_alter_table("shifts", schema="workforce") as batch_op:
        batch_op.create_index("uq_shifts_company_name", ["company_id", sa.literal_column("lower(name)")], unique=True)

    op.create_table(
        "work_sites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("radius_m", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("street", sa.String(length=150), nullable=True),
        sa.Column("exterior_number", sa.String(length=20), nullable=True),
        sa.Column("interior_number", sa.String(length=20), nullable=True),
        sa.Column("postal_code", sa.String(length=10), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("state", sa.String(length=100), nullable=True),
        sa.Column("municipality", sa.String(length=100), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("latitude", sa.Double(), nullable=True),
        sa.Column("longitude", sa.Double(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "latitude IS NOT NULL AND longitude IS NOT NULL AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180",
            name=op.f("ck_work_sites_coordinates"),
        ),
        sa.CheckConstraint("radius_m BETWEEN 10 AND 10000", name=op.f("ck_work_sites_radius")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_work_sites_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["country_code"],
            ["catalog.countries.code"],
            name=op.f("fk_work_sites_country_code_countries"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_sites")),
        sa.UniqueConstraint("id", "company_id", name="uq_work_sites_id_company"),
        schema="workforce",
    )
    with op.batch_alter_table("work_sites", schema="workforce") as batch_op:
        batch_op.create_index("ix_work_sites_country_code", ["country_code"], unique=False)
        batch_op.create_index(
            "uq_work_sites_company_name", ["company_id", sa.literal_column("lower(name)")], unique=True
        )

    op.create_table(
        "shift_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("shift_id", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("remote_weekdays", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("remote_weekdays BETWEEN 0 AND 127", name=op.f("ck_shift_assignments_remote_weekdays")),
        sa.CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name=op.f("ck_shift_assignments_dates")),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["auth.users.id"],
            name=op.f("fk_shift_assignments_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id", "company_id"],
            ["workforce.employees.id", "workforce.employees.company_id"],
            name="fk_shift_assignments_employee_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shift_id", "company_id"],
            ["workforce.shifts.id", "workforce.shifts.company_id"],
            name="fk_shift_assignments_shift_company",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shift_assignments")),
        sa.UniqueConstraint("id", "company_id", name="uq_shift_assignments_id_company"),
        schema="workforce",
    )
    with op.batch_alter_table("shift_assignments", schema="workforce") as batch_op:
        batch_op.create_index("ix_shift_assignments_company", ["company_id"], unique=False)
        batch_op.create_index(
            "ix_shift_assignments_created_by_id",
            ["created_by_id"],
            unique=False,
            postgresql_where=sa.text("created_by_id IS NOT NULL"),
            sqlite_where=sa.text("created_by_id IS NOT NULL"),
        )
        batch_op.create_index("ix_shift_assignments_employee_from", ["employee_id", "valid_from"], unique=False)
        batch_op.create_index("ix_shift_assignments_shift", ["shift_id"], unique=False)

    op.create_table(
        "shift_change_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("shift_id", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["employee_id", "company_id"],
            ["workforce.employees.id", "workforce.employees.company_id"],
            name="fk_shift_change_requests_employee_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"],
            ["auth.users.id"],
            name=op.f("fk_shift_change_requests_reviewed_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["shift_id", "company_id"],
            ["workforce.shifts.id", "workforce.shifts.company_id"],
            name="fk_shift_change_requests_shift_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["status"],
            ["catalog.shift_request_statuses.code"],
            name=op.f("fk_shift_change_requests_status_shift_request_statuses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shift_change_requests")),
        schema="workforce",
    )
    with op.batch_alter_table("shift_change_requests", schema="workforce") as batch_op:
        batch_op.create_index("ix_shift_change_requests_company_status", ["company_id", "status", "id"], unique=False)
        batch_op.create_index("ix_shift_change_requests_employee", ["employee_id", "id"], unique=False)
        batch_op.create_index(
            "ix_shift_change_requests_reviewed_by_id",
            ["reviewed_by_id"],
            unique=False,
            postgresql_where=sa.text("reviewed_by_id IS NOT NULL"),
            sqlite_where=sa.text("reviewed_by_id IS NOT NULL"),
        )
        batch_op.create_index("ix_shift_change_requests_shift", ["shift_id"], unique=False)
        batch_op.create_index("ix_shift_change_requests_status", ["status"], unique=False)
        batch_op.create_index(
            "uq_shift_change_requests_pending",
            ["employee_id"],
            unique=True,
            postgresql_where=sa.text("status = 'PENDING'"),
            sqlite_where=sa.text("status = 'PENDING'"),
        )

    op.create_table(
        "work_sessions",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("assignment_id", sa.Integer(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("shift_name", sa.String(length=80), nullable=False),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("check_out_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("breaks_allowed", sa.Integer(), nullable=False),
        sa.Column("break_minutes_allowed", sa.Integer(), nullable=False),
        sa.Column("early_check_out_minutes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("check_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("check_in_mode", sa.String(length=20), nullable=False),
        sa.Column("check_in_site_id", sa.Integer(), nullable=True),
        sa.Column("check_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_out_mode", sa.String(length=20), nullable=True),
        sa.Column("check_out_site_id", sa.Integer(), nullable=True),
        sa.Column("late_minutes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("early_leave_minutes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("break_minutes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("worked_minutes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "check_out_at IS NULL OR check_out_at >= check_in_at", name=op.f("ck_work_sessions_check_out")
        ),
        sa.CheckConstraint("scheduled_end > scheduled_start", name=op.f("ck_work_sessions_schedule")),
        sa.ForeignKeyConstraint(
            ["assignment_id", "company_id"],
            ["workforce.shift_assignments.id", "workforce.shift_assignments.company_id"],
            name="fk_work_sessions_assignment_company",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["check_in_mode"], ["catalog.work_modes.code"], name=op.f("fk_work_sessions_check_in_mode_work_modes")
        ),
        sa.ForeignKeyConstraint(
            ["check_in_site_id", "company_id"],
            ["workforce.work_sites.id", "workforce.work_sites.company_id"],
            name="fk_work_sessions_check_in_site_company",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["check_out_mode"], ["catalog.work_modes.code"], name=op.f("fk_work_sessions_check_out_mode_work_modes")
        ),
        sa.ForeignKeyConstraint(
            ["check_out_site_id", "company_id"],
            ["workforce.work_sites.id", "workforce.work_sites.company_id"],
            name="fk_work_sessions_check_out_site_company",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id", "company_id"],
            ["workforce.employees.id", "workforce.employees.company_id"],
            name="fk_work_sessions_employee_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["status"],
            ["catalog.work_session_statuses.code"],
            name=op.f("fk_work_sessions_status_work_session_statuses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_sessions")),
        schema="attendance",
    )
    with op.batch_alter_table("work_sessions", schema="attendance") as batch_op:
        batch_op.create_index("ix_work_sessions_assignment", ["assignment_id"], unique=False)
        batch_op.create_index("ix_work_sessions_check_in_mode", ["check_in_mode"], unique=False)
        batch_op.create_index("ix_work_sessions_check_in_site", ["check_in_site_id"], unique=False)
        batch_op.create_index("ix_work_sessions_check_out_mode", ["check_out_mode"], unique=False)
        batch_op.create_index("ix_work_sessions_check_out_site", ["check_out_site_id"], unique=False)
        batch_op.create_index("ix_work_sessions_company_date", ["company_id", "work_date", "id"], unique=False)
        batch_op.create_index(
            "ix_work_sessions_open_deadline",
            ["check_out_deadline"],
            unique=False,
            postgresql_where=sa.text("status = 'OPEN'"),
            sqlite_where=sa.text("status = 'OPEN'"),
        )
        batch_op.create_index("ix_work_sessions_status", ["status"], unique=False)
        batch_op.create_index(
            "uq_work_sessions_employee_open",
            ["employee_id"],
            unique=True,
            postgresql_where=sa.text("status = 'OPEN'"),
            sqlite_where=sa.text("status = 'OPEN'"),
        )
        batch_op.create_index("uq_work_sessions_employee_start", ["employee_id", "scheduled_start"], unique=True)

    op.create_table(
        "shift_assignment_sites",
        sa.Column("assignment_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assignment_id", "company_id"],
            ["workforce.shift_assignments.id", "workforce.shift_assignments.company_id"],
            name="fk_shift_assignment_sites_assignment_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "company_id"],
            ["workforce.work_sites.id", "workforce.work_sites.company_id"],
            name="fk_shift_assignment_sites_site_company",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("assignment_id", "site_id", name=op.f("pk_shift_assignment_sites")),
        schema="workforce",
    )
    with op.batch_alter_table("shift_assignment_sites", schema="workforce") as batch_op:
        batch_op.create_index("ix_shift_assignment_sites_site", ["site_id"], unique=False)

    op.create_table(
        "attendance_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latitude", sa.Double(), nullable=True),
        sa.Column("longitude", sa.Double(), nullable=True),
        sa.Column("accuracy_m", sa.Double(), nullable=True),
        sa.Column("distance_m", sa.Double(), nullable=True),
        sa.Column("verification_log_id", sa.Integer(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL) AND (latitude IS NULL OR (latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180))",
            name=op.f("ck_attendance_events_coordinates"),
        ),
        sa.ForeignKeyConstraint(
            ["action"], ["catalog.attendance_actions.code"], name=op.f("fk_attendance_events_action_attendance_actions")
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["auth.users.id"], name=op.f("fk_attendance_events_actor_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["workforce.employees.id"],
            name=op.f("fk_attendance_events_employee_id_employees"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mode"], ["catalog.work_modes.code"], name=op.f("fk_attendance_events_mode_work_modes")
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["attendance.work_sessions.id"],
            name=op.f("fk_attendance_events_session_id_work_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "company_id"],
            ["workforce.work_sites.id", "workforce.work_sites.company_id"],
            name="fk_attendance_events_site_company",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["verification_log_id"],
            ["attendance.verification_logs.id"],
            name=op.f("fk_attendance_events_verification_log_id_verification_logs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attendance_events")),
        schema="attendance",
    )
    with op.batch_alter_table("attendance_events", schema="attendance") as batch_op:
        batch_op.create_index("ix_attendance_events_action", ["action"], unique=False)
        batch_op.create_index(
            "ix_attendance_events_actor_id",
            ["actor_id"],
            unique=False,
            postgresql_where=sa.text("actor_id IS NOT NULL"),
            sqlite_where=sa.text("actor_id IS NOT NULL"),
        )
        batch_op.create_index("ix_attendance_events_company_occurred", ["company_id", "occurred_at"], unique=False)
        batch_op.create_index("ix_attendance_events_employee", ["employee_id", "id"], unique=False)
        batch_op.create_index("ix_attendance_events_mode", ["mode"], unique=False)
        batch_op.create_index("ix_attendance_events_session", ["session_id", "id"], unique=False)
        batch_op.create_index("ix_attendance_events_site", ["site_id"], unique=False)
        batch_op.create_index(
            "ix_attendance_events_verification_log_id",
            ["verification_log_id"],
            unique=False,
            postgresql_where=sa.text("verification_log_id IS NOT NULL"),
            sqlite_where=sa.text("verification_log_id IS NOT NULL"),
        )

    op.create_table(
        "work_breaks",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exceeded_minutes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name=op.f("ck_work_breaks_times")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_work_breaks_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["attendance.work_sessions.id"],
            name=op.f("fk_work_breaks_session_id_work_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_breaks")),
        schema="attendance",
    )
    with op.batch_alter_table("work_breaks", schema="attendance") as batch_op:
        batch_op.create_index("ix_work_breaks_company", ["company_id"], unique=False)
        batch_op.create_index("ix_work_breaks_session", ["session_id", "id"], unique=False)
        batch_op.create_index(
            "uq_work_breaks_session_open",
            ["session_id"],
            unique=True,
            postgresql_where=sa.text("ended_at IS NULL"),
            sqlite_where=sa.text("ended_at IS NULL"),
        )


def _catalogs(seed: dict) -> None:
    for catalog in CATALOGS:
        table = sa.table(catalog, *(sa.column(key) for key in seed[catalog][0]), schema="catalog")
        for row in seed[catalog]:
            op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=["code"]))


def _screens(seed: dict) -> None:
    screens = sa.table(
        "screens", sa.column("code"), sa.column("sort_order"), sa.column("description"), schema="catalog"
    )
    for row in seed["screens"]:  # el orden del menú y las descripciones del seed
        op.execute(
            sa.update(screens)
            .where(screens.c.code == row["code"])
            .values(sort_order=row["sort_order"], description=row["description"])
        )
    for row in (r for r in seed["screens"] if r["code"] in SCREENS):
        table = sa.table("screens", *(sa.column(key) for key in row), schema="catalog")
        op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=["code"]))
    grants = sa.table("role_screens", sa.column("role_code"), sa.column("screen_code"), schema="catalog")
    for grant in (g for g in seed["role_screens"] if g["screen_code"] in SCREENS):
        op.execute(
            postgresql.insert(grants)
            .values(**grant)
            .on_conflict_do_nothing(index_elements=["role_code", "screen_code"])
        )


def _policy() -> None:
    op.add_column(
        "verification_policy",
        sa.Column("max_location_accuracy_m", sa.SmallInteger(), server_default=sa.text("100"), nullable=False),
        schema="tenancy",
    )
    op.add_column(
        "verification_policy",
        sa.Column("detect_impossible_travel", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        schema="tenancy",
    )
    op.add_column(
        "verification_policy",
        sa.Column("max_travel_kmh", sa.SmallInteger(), server_default=sa.text("200"), nullable=False),
        schema="tenancy",
    )
    for name, condition in POLICY_CHECKS:
        op.create_check_constraint(op.f(name), "verification_policy", condition, schema="tenancy")
    op.execute(f"ALTER TABLE tenancy.verification_policy RENAME CONSTRAINT {QUALITY_CHECK_0041} TO {QUALITY_CHECK}")


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    _tables()
    _catalogs(seed)
    _screens(seed)
    _policy()


def downgrade() -> None:
    grants = sa.table("role_screens", sa.column("screen_code"), schema="catalog")
    op.execute(sa.delete(grants).where(grants.c.screen_code.in_(SCREENS)))
    screens = sa.table("screens", sa.column("code"), schema="catalog")
    op.execute(sa.delete(screens).where(screens.c.code.in_(SCREENS)))
    op.execute(f"ALTER TABLE tenancy.verification_policy RENAME CONSTRAINT {QUALITY_CHECK} TO {QUALITY_CHECK_0041}")
    for name, _condition in POLICY_CHECKS:
        op.drop_constraint(op.f(name), "verification_policy", schema="tenancy", type_="check")
    for column in ("max_travel_kmh", "detect_impossible_travel", "max_location_accuracy_m"):
        op.drop_column("verification_policy", column, schema="tenancy")
    for schema, table in TABLES:
        op.drop_table(table, schema=schema)
