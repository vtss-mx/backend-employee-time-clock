"""Asistencia por turno: la jornada de cada turno, sus descansos y la bitácora de cada registro.

- `WorkSession`: la jornada de UN turno de un empleado (entrada, salida y sus cálculos). Guarda el
  horario programado con el que ocurrió: cambiar el turno después no altera lo ya registrado.
- `WorkBreak`: cada descanso de la jornada.
- `AttendanceEvent`: bitácora de solo inserción de cada registro (entrada, descansos, salida) con la
  hora del servidor, la ubicación enviada, el sitio, la modalidad y la verificación facial que lo
  respaldó. Es la evidencia de cada jornada.

La empresa puede registrar la jornada de quien no checó o corregirla (`attendance_manual`): la
jornada guarda quién, cuándo y por qué (`edited_by_id`, `edited_at`, `edit_reason`) y la bitácora
suma registros con la modalidad COMPANY y el motivo (los anteriores se conservan como evidencia).
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import ATTENDANCE, AUTH, CATALOG, WORKFORCE

_BIG_ID = BigInteger().with_variant(Integer, "sqlite")


def _site_fk(table: str, column: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        [column, "company_id"],
        [f"{WORKFORCE}.work_sites.id", f"{WORKFORCE}.work_sites.company_id"],
        name=f"fk_{table}_{column.removesuffix('_id')}_company",
        ondelete="RESTRICT",
    )


class WorkSession(Base):
    """La jornada de un turno: de la entrada a la salida (o sin salida si venció su límite)."""

    __tablename__ = "work_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["employee_id", "company_id"],
            [f"{WORKFORCE}.employees.id", f"{WORKFORCE}.employees.company_id"],
            name="fk_work_sessions_employee_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["assignment_id", "company_id"],
            [f"{WORKFORCE}.shift_assignments.id", f"{WORKFORCE}.shift_assignments.company_id"],
            name="fk_work_sessions_assignment_company",
            ondelete="RESTRICT",
        ),
        _site_fk("work_sessions", "check_in_site_id"),
        _site_fk("work_sessions", "check_out_site_id"),
        # Un turno se registra una sola vez por día; y a lo más una jornada abierta por empleado (la
        # base lo garantiza también entre dos registros simultáneos).
        Index("uq_work_sessions_employee_start", "employee_id", "scheduled_start", unique=True),
        Index(
            "uq_work_sessions_employee_open",
            "employee_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
            sqlite_where=text("status = 'OPEN'"),
        ),
        # Súper-índice del día de la empresa (migración 0047): el historial en su orden exacto (fecha,
        # entrada, id) sin ordenar, y los conteos del tablero y "¿registró su jornada?" de un día sin leer
        # la tabla (INCLUDE status, employee_id).
        Index(
            "ix_work_sessions_company_date",
            "company_id",
            "work_date",
            "scheduled_start",
            "id",
            postgresql_include=["status", "employee_id"],
        ),
        # El mantenimiento busca las abiertas vencidas.
        Index(
            "ix_work_sessions_open_deadline",
            "check_out_deadline",
            postgresql_where=text("status = 'OPEN'"),
            sqlite_where=text("status = 'OPEN'"),
        ),
        # FK con RESTRICT (borrar una asignación o un sitio revisa que nadie la use). Los sitios, parciales:
        # una jornada remota (o aún abierta) no tiene sitio y no ocupa lugar en el índice.
        Index("ix_work_sessions_assignment", "assignment_id"),
        Index(
            "ix_work_sessions_check_in_site",
            "check_in_site_id",
            postgresql_where=text("check_in_site_id IS NOT NULL"),
            sqlite_where=text("check_in_site_id IS NOT NULL"),
        ),
        Index(
            "ix_work_sessions_check_out_site",
            "check_out_site_id",
            postgresql_where=text("check_out_site_id IS NOT NULL"),
            sqlite_where=text("check_out_site_id IS NOT NULL"),
        ),
        Index(
            "ix_work_sessions_edited_by_id",
            "edited_by_id",
            postgresql_where=text("edited_by_id IS NOT NULL"),
            sqlite_where=text("edited_by_id IS NOT NULL"),
        ),
        CheckConstraint("scheduled_end > scheduled_start", name="schedule"),
        CheckConstraint("check_out_at IS NULL OR check_out_at >= check_in_at", name="check_out"),
        {"schema": ATTENDANCE},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    employee_id: Mapped[int] = mapped_column(nullable=False)
    assignment_id: Mapped[int] = mapped_column(nullable=False)
    #: Día del calendario (hora del negocio) en que empieza el turno: un turno nocturno es del día en
    #: que entra aunque salga al siguiente.
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    shift_name: Mapped[str] = mapped_column(String(80), nullable=False)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Hasta cuándo se puede checar la salida; después la jornada queda sin salida.
    check_out_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Descansos permitidos y minutos de cada uno (del turno al momento de entrar).
    breaks_allowed: Mapped[int] = mapped_column(Integer, nullable=False)
    break_minutes_allowed: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Minutos antes de la salida que no cuentan como salida anticipada (del turno al entrar).
    early_check_out_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), ForeignKey(f"{CATALOG}.work_session_statuses.code"), default="OPEN", nullable=False
    )
    check_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    check_in_mode: Mapped[str] = mapped_column(String(20), ForeignKey(f"{CATALOG}.work_modes.code"), nullable=False)
    check_in_site_id: Mapped[int | None] = mapped_column()
    check_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_out_mode: Mapped[str | None] = mapped_column(String(20), ForeignKey(f"{CATALOG}.work_modes.code"))
    check_out_site_id: Mapped[int | None] = mapped_column()
    #: Minutos de retardo (más allá de la tolerancia) y de salida anticipada.
    late_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    early_leave_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    #: Minutos en descanso y minutos trabajados (al checar la salida: de la entrada a la salida sin descansos).
    break_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    worked_minutes: Mapped[int | None] = mapped_column(Integer)
    #: La empresa la registró o la corrigió: quién, cuándo y por qué (None: solo registros del empleado).
    edited_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edit_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkBreak(Base):
    """Un descanso de la jornada."""

    __tablename__ = "work_breaks"
    __table_args__ = (
        # Un solo descanso abierto por jornada.
        Index(
            "uq_work_breaks_session_open",
            "session_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
        Index("ix_work_breaks_session", "session_id", "id"),
        Index("ix_work_breaks_company", "company_id"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="times"),
        {"schema": ATTENDANCE},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("tenancy.companies.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[int] = mapped_column(
        _BIG_ID, ForeignKey(f"{ATTENDANCE}.work_sessions.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Minutos por encima de lo permitido para cada descanso (al terminarlo).
    exceeded_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)


class AttendanceEvent(Base):
    """Bitácora de solo inserción: cada registro de asistencia con su evidencia."""

    __tablename__ = "attendance_events"
    __table_args__ = (
        _site_fk("attendance_events", "site_id"),
        # La última ubicación del empleado (viaje imposible) y la evidencia de una jornada.
        Index("ix_attendance_events_employee", "employee_id", "id"),
        Index("ix_attendance_events_session", "session_id", "id"),
        # FK con RESTRICT hacia el sitio (parcial: un registro remoto no tiene sitio).
        Index(
            "ix_attendance_events_site",
            "site_id",
            postgresql_where=text("site_id IS NOT NULL"),
            sqlite_where=text("site_id IS NOT NULL"),
        ),
        Index(
            "ix_attendance_events_verification_log_id",
            "verification_log_id",
            postgresql_where=text("verification_log_id IS NOT NULL"),
            sqlite_where=text("verification_log_id IS NOT NULL"),
        ),
        Index(
            "ix_attendance_events_actor_id",
            "actor_id",
            postgresql_where=text("actor_id IS NOT NULL"),
            sqlite_where=text("actor_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL) AND "
            "(latitude IS NULL OR (latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180))",
            name="coordinates",
        ),
        {"schema": ATTENDANCE},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey(f"{WORKFORCE}.employees.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[int] = mapped_column(
        _BIG_ID, ForeignKey(f"{ATTENDANCE}.work_sessions.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(20), ForeignKey(f"{CATALOG}.attendance_actions.code"), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), ForeignKey(f"{CATALOG}.work_modes.code"), nullable=False)
    site_id: Mapped[int | None] = mapped_column()
    #: Hora del servidor (nunca la del dispositivo).
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)
    #: Precisión informada por el dispositivo (m) y distancia al sitio (m) si fue en sitio.
    accuracy_m: Mapped[float | None] = mapped_column(Double)
    distance_m: Mapped[float | None] = mapped_column(Double)
    #: La verificación facial que respaldó el registro (o la identificación del validador).
    verification_log_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{ATTENDANCE}.verification_logs.id", ondelete="SET NULL")
    )
    #: Quién operó: el propio empleado, la cuenta del validador o la de la empresa (modalidad COMPANY).
    actor_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    #: Motivo de un registro de la empresa (lo ve también el empleado en su historial).
    note: Mapped[str | None] = mapped_column(String(500))
