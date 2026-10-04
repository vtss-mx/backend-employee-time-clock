"""Calendario de días libres y asistencia registrada por la empresa

El dueño del producto pidió programar qué días se trabaja y cuáles no, y que en un día libre nadie
pueda checar (el servidor lo rechaza con el motivo). Si la empresa necesita a alguien ese día, se lo
marca como laborable solo a esa persona.

- `catalog.day_off_types`: tipos de ausencia (vacaciones y permiso los puede pedir el empleado;
  incapacidad y otro motivo solo los registra la empresa) con la frase que se le dice al empleado.
- `workforce.company_holidays`: días festivos de la empresa (los oficiales de la Ley Federal del
  Trabajo se agregan con un botón; la empresa agrega o quita los suyos).
- `workforce.employee_absences`: ausencias de cada empleado. Reutilizan los estados de
  `shift_request_statuses` (pendiente, aprobada, rechazada, cancelada): una solicitud del empleado
  sigue el mismo ciclo que un cambio de turno. Un índice parcial (pendientes y aprobadas) sirve para
  no encimarlas y para saber, en una consulta, quién no trabaja un día.
- `workforce.employee_workdays`: "esta persona sí trabaja este día" aunque sea festivo o esté de
  vacaciones.
- `board_states`: `DAY_OFF` (el tablero muestra "Día libre" en lugar de "Faltó").
- Asistencia registrada o corregida por la empresa (solo la empresa, sin rostro ni ubicación): la
  modalidad `COMPANY` en `work_modes`, los motivos sugeridos `catalog.attendance_edit_reasons`, quién,
  cuándo y por qué en `attendance.work_sessions` (`edited_by_id`, `edited_at`, `edit_reason`) y el
  motivo de cada registro en `attendance.attendance_events.note`.
- Pantalla `COMPANY_CALENDAR` ("Calendario", módulo Asistencia, después de Turnos) con su permiso;
  el menú se renumera y las descripciones quedan como en el seed (Turnos ya asigna a varios
  empleados; Mi asistencia incluye vacaciones y permisos; los estados de solicitud ya no hablan solo
  de cambios de turno).

No modifica datos de las empresas.

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-05 00:30:00
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
SCREEN = "COMPANY_CALENDAR"
BOARD_STATE = "DAY_OFF"
WORK_MODE = "COMPANY"
#: La pantalla nueva va después de Turnos (orden 10): las que seguían bajan un lugar.
FIRST_MOVED = 10
#: Textos anteriores (para revertir): (tabla, código, descripción).
PREVIOUS_DESCRIPTIONS = (
    ("shift_request_statuses", "APPROVED", "La empresa aprobó el cambio: el nuevo turno aplica desde la fecha pedida."),
    ("shift_request_statuses", "REJECTED", "La empresa no aprobó el cambio (con su motivo)."),
    ("shift_request_statuses", "CANCELLED", "El empleado la retiró antes de que se decidiera."),
    ("menu_modules", "ATTENDANCE", "El día a día: tablero, turnos y sitios donde se checa."),
    ("screens", "COMPANY_SHIFTS", "Turnos de trabajo, su asignación a empleados y las solicitudes de cambio de turno."),
    ("screens", "EMPLOYEE_ATTENDANCE", "Entrada, descansos y salida de su turno, con su rostro y su ubicación."),
)
TABLES = ("employee_workdays", "employee_absences", "company_holidays")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["auth.users.id"], name=op.f(f"fk_{table}_{column}_users"), ondelete="SET NULL"
    )


def _employee_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["employee_id", "company_id"],
        ["workforce.employees.id", "workforce.employees.company_id"],
        name=f"fk_{table}_employee_company",
        ondelete="CASCADE",
    )


def _user_index(batch_op: Any, table: str, column: str) -> None:
    batch_op.create_index(
        f"ix_{table}_{column}",
        [column],
        unique=False,
        postgresql_where=sa.text(f"{column} IS NOT NULL"),
        sqlite_where=sa.text(f"{column} IS NOT NULL"),
    )


def _day_off_types() -> sa.Table:
    return op.create_table(
        "day_off_types",
        sa.Column("phrase", sa.String(length=80), nullable=False),
        sa.Column("requestable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("tone", sa.String(length=20), server_default="muted", nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_day_off_types")),
        schema="catalog",
    )


def _holidays() -> None:
    op.create_table(
        "company_holidays",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("official", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_company_holidays_company_id_companies"),
            ondelete="CASCADE",
        ),
        _user_fk("company_holidays", "created_by_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_holidays")),
        sa.UniqueConstraint("company_id", "holiday_date", name="uq_company_holidays_company_date"),
        schema="workforce",
    )
    with op.batch_alter_table("company_holidays", schema="workforce") as batch_op:
        _user_index(batch_op, "company_holidays", "created_by_id")


def _absences() -> None:
    op.create_table(
        "employee_absences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("type_code", sa.String(length=30), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("requested_by_id", sa.Integer(), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(length=500), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "ends_on >= starts_on AND ends_on - starts_on < 366", name=op.f("ck_employee_absences_dates")
        ),
        _employee_fk("employee_absences"),
        sa.ForeignKeyConstraint(
            ["type_code"],
            ["catalog.day_off_types.code"],
            name=op.f("fk_employee_absences_type_code_day_off_types"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["status"],
            ["catalog.shift_request_statuses.code"],
            name=op.f("fk_employee_absences_status_shift_request_statuses"),
            ondelete="RESTRICT",
        ),
        _user_fk("employee_absences", "requested_by_id"),
        _user_fk("employee_absences", "decided_by_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_absences")),
        schema="workforce",
    )
    with op.batch_alter_table("employee_absences", schema="workforce") as batch_op:
        batch_op.create_index("ix_employee_absences_employee", ["employee_id", "starts_on"], unique=False)
        batch_op.create_index(
            "ix_employee_absences_employee_active",
            ["employee_id", "starts_on", "ends_on"],
            unique=False,
            postgresql_where=sa.text("status IN ('PENDING', 'APPROVED')"),
            sqlite_where=sa.text("status IN ('PENDING', 'APPROVED')"),
        )
        batch_op.create_index("ix_employee_absences_company_starts", ["company_id", "starts_on", "id"], unique=False)
        batch_op.create_index("ix_employee_absences_company_status", ["company_id", "status", "id"], unique=False)
        batch_op.create_index("ix_employee_absences_type_code", ["type_code"], unique=False)
        batch_op.create_index("ix_employee_absences_status", ["status"], unique=False)
        _user_index(batch_op, "employee_absences", "requested_by_id")
        _user_index(batch_op, "employee_absences", "decided_by_id")


def _workdays() -> None:
    op.create_table(
        "employee_workdays",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        *_timestamps(),
        _employee_fk("employee_workdays"),
        _user_fk("employee_workdays", "created_by_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_workdays")),
        sa.UniqueConstraint("employee_id", "work_date", name="uq_employee_workdays_employee_date"),
        schema="workforce",
    )
    with op.batch_alter_table("employee_workdays", schema="workforce") as batch_op:
        batch_op.create_index("ix_employee_workdays_company_date", ["company_id", "work_date", "id"], unique=False)
        _user_index(batch_op, "employee_workdays", "created_by_id")


def _insert_missing(table_name: str, row: dict, keys: list[str]) -> None:
    """Inserta la fila del seed si no está (una base nueva ya la tiene: 0021/0042/0043 copian el seed)."""
    table = sa.table(table_name, *(sa.column(key) for key in row), schema="catalog")
    op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=keys))


def _set_description(table_name: str, code: str, description: str) -> None:
    table = sa.table(table_name, sa.column("code"), sa.column("description"), schema="catalog")
    op.execute(sa.update(table).where(table.c.code == code).values(description=description))


def _edit_reasons() -> sa.Table:
    return op.create_table(
        "attendance_edit_reasons",
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_attendance_edit_reasons")),
        schema="catalog",
    )


def _company_edits() -> None:
    """Quién, cuándo y por qué la empresa registró o corrigió una jornada; el motivo de cada registro."""
    with op.batch_alter_table("work_sessions", schema="attendance") as batch_op:
        batch_op.add_column(sa.Column("edited_by_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("edit_reason", sa.String(length=500), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_work_sessions_edited_by_id_users"), "users", ["edited_by_id"], ["id"],
            referent_schema="auth", ondelete="SET NULL",
        )
        _user_index(batch_op, "work_sessions", "edited_by_id")
    with op.batch_alter_table("attendance_events", schema="attendance") as batch_op:
        batch_op.add_column(sa.Column("note", sa.String(length=500), nullable=True))


def _catalogs(seed: dict) -> None:
    op.bulk_insert(_day_off_types(), seed["day_off_types"])
    op.bulk_insert(_edit_reasons(), seed["attendance_edit_reasons"])
    _insert_missing("board_states", next(r for r in seed["board_states"] if r["code"] == BOARD_STATE), ["code"])
    _insert_missing("work_modes", next(r for r in seed["work_modes"] if r["code"] == WORK_MODE), ["code"])
    for table_name, code, _previous in PREVIOUS_DESCRIPTIONS:
        if table_name != "screens":
            row = next(r for r in seed[table_name] if r["code"] == code)
            _set_description(table_name, code, row["description"])


def _screens(seed: dict) -> None:
    _insert_missing("screens", next(r for r in seed["screens"] if r["code"] == SCREEN), ["code"])
    for grant in (g for g in seed["role_screens"] if g["screen_code"] == SCREEN):
        _insert_missing("role_screens", grant, ["role_code", "screen_code"])
    for link in (m for m in seed["menu_module_screens"] if m["screen_code"] == SCREEN):
        _insert_missing("menu_module_screens", link, ["screen_code"])
    screens = sa.table(
        "screens", sa.column("code"), sa.column("sort_order"), sa.column("description"), schema="catalog"
    )
    for row in seed["screens"]:  # el orden del menú y las descripciones del seed
        op.execute(
            sa.update(screens)
            .where(screens.c.code == row["code"])
            .values(sort_order=row["sort_order"], description=row["description"])
        )


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    _catalogs(seed)
    _holidays()
    _absences()
    _workdays()
    _company_edits()
    _screens(seed)


def downgrade() -> None:
    with op.batch_alter_table("attendance_events", schema="attendance") as batch_op:
        batch_op.drop_column("note")
    with op.batch_alter_table("work_sessions", schema="attendance") as batch_op:
        batch_op.drop_index("ix_work_sessions_edited_by_id")
        batch_op.drop_constraint(op.f("fk_work_sessions_edited_by_id_users"), type_="foreignkey")
        for column in ("edit_reason", "edited_at", "edited_by_id"):
            batch_op.drop_column(column)
    # La modalidad COMPANY se conserva: las jornadas que registró la empresa la siguen nombrando.
    for table_name in TABLES:
        op.drop_table(table_name, schema="workforce")
    op.drop_table("day_off_types", schema="catalog")
    op.drop_table("attendance_edit_reasons", schema="catalog")
    for table_name, code in (("menu_module_screens", "screen_code"), ("role_screens", "screen_code")):
        links = sa.table(table_name, sa.column(code), schema="catalog")
        op.execute(sa.delete(links).where(links.c[code] == SCREEN))
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    op.execute(sa.delete(screens).where(screens.c.code == SCREEN))
    op.execute(
        sa.update(screens).where(screens.c.sort_order > FIRST_MOVED).values(sort_order=screens.c.sort_order - 1)
    )
    states = sa.table("board_states", sa.column("code"), schema="catalog")
    op.execute(sa.delete(states).where(states.c.code == BOARD_STATE))
    for table_name, code, previous in PREVIOUS_DESCRIPTIONS:
        _set_description(table_name, code, previous)
