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
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import ATTENDANCE, AUTH, CATALOG, WORKFORCE
from app.core.partitions import partitioned
from app.models.mixins import company_fk

_BIG_ID = BigInteger().with_variant(Integer, "sqlite")


def _employee_fk(table: str) -> ForeignKeyConstraint:
    return company_fk(table, "employee_id", f"{WORKFORCE}.employees")


def _session_fk(table: str) -> ForeignKeyConstraint:
    return company_fk(table, "session_id", f"{ATTENDANCE}.work_sessions")


def _site_fk(table: str, column: str) -> ForeignKeyConstraint:
    return company_fk(table, column, f"{WORKFORCE}.work_sites", ondelete="RESTRICT")


class WorkSession(Base):
    """La jornada de un turno: de la entrada a la salida (o sin salida si venció su límite)."""

    __tablename__ = "work_sessions"
    __table_args__ = (
        _employee_fk("work_sessions"),
        # Destino de las FK compuestas de descansos y registros: la jornada y su empresa van juntas.
        UniqueConstraint("id", "company_id", name="uq_work_sessions_id_company"),
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
        # Registros "en revisión" (motor de riesgo, migración 0062): la bandeja de la empresa en el orden del
        # historial y el contador del menú, sin leer las jornadas que no están pendientes (casi todas).
        Index(
            "ix_work_sessions_review_pending",
            "company_id",
            "work_date",
            "scheduled_start",
            "id",
            postgresql_where=text("review_status = 'PENDING'"),
            sqlite_where=text("review_status = 'PENDING'"),
        ),
        Index(
            "ix_work_sessions_reviewed_by_id",
            "reviewed_by_id",
            postgresql_where=text("reviewed_by_id IS NOT NULL"),
            sqlite_where=text("reviewed_by_id IS NOT NULL"),
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
    #: "En revisión" (motor de riesgo, decisión D3): algún registro de la jornada tuvo riesgo alto y quedó guardado
    #: pendiente de que la empresa lo confirme o lo rechace (catalog.attendance_review_statuses); None = nada que
    #: revisar. `review_reasons`: códigos de catalog.review_reasons (en términos del negocio) separados por coma.
    review_status: Mapped[str | None] = mapped_column(
        String(20), ForeignKey(f"{CATALOG}.attendance_review_statuses.code")
    )
    review_reasons: Mapped[str | None] = mapped_column(String(200))
    #: Quién de la empresa lo confirmó o rechazó, cuándo y su nota (obligatoria al rechazar).
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(String(500))
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
        _session_fk("work_breaks"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="times"),
        {"schema": ATTENDANCE},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("tenancy.companies.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[int] = mapped_column(_BIG_ID, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Minutos por encima de lo permitido para cada descanso (al terminarlo).
    exceeded_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)


class AttendanceEvent(Base):
    """Bitácora de solo inserción: cada registro de asistencia con su evidencia.

    Crece sin límite: particionada por mes en `occurred_at` (`app/core/partitions.py`; en PostgreSQL la llave
    primaria es `(id, occurred_at)`). Empleado, jornada y sitio son de la MISMA empresa (FK compuestas).
    `verification_log_id` es una referencia SIN llave foránea: la bitácora también está particionada y una FK
    hacia ella exigiría copiar su fecha aquí (y le impediría borrar meses viejos); ambas filas se escriben en la
    misma transacción y se borran juntas con el empleado o la empresa (CASCADE de cada una).
    """

    __tablename__ = "attendance_events"
    __table_args__ = (
        _employee_fk("attendance_events"),
        _session_fk("attendance_events"),
        _site_fk("attendance_events", "site_id"),
        # La última ubicación del empleado (viaje imposible: los registros de la ventana en que un viaje aún
        # podría ser imposible, en el orden del índice; con la fecha, solo las particiones de esa ventana).
        Index("ix_attendance_events_employee", "employee_id", "occurred_at", "id"),
        # La evidencia de una jornada.
        Index("ix_attendance_events_session", "session_id", "id"),
        # FK compuesta con RESTRICT hacia el sitio y "¿ya se checó en el sitio?" sin leer la tabla (también con la
        # seguridad por fila, que pide company_id). Parcial: un registro remoto no tiene sitio.
        Index(
            "ix_attendance_events_site",
            "company_id",
            "site_id",
            postgresql_where=text("site_id IS NOT NULL"),
            sqlite_where=text("site_id IS NOT NULL"),
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
        {"schema": ATTENDANCE, **partitioned("occurred_at")},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    employee_id: Mapped[int] = mapped_column(nullable=False)
    session_id: Mapped[int] = mapped_column(_BIG_ID, nullable=False)
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
    verification_log_id: Mapped[int | None] = mapped_column(Integer)
    #: Quién operó: el propio empleado, la cuenta del validador o la de la empresa (modalidad COMPANY).
    actor_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    #: Motivo de un registro de la empresa (lo ve también el empleado en su historial).
    note: Mapped[str | None] = mapped_column(String(500))
    #: Este registro se guardó "en revisión" (riesgo alto del motor de riesgo): su jornada lo dice en `review_status`.
    under_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: País y sistema autónomo de la IP del registro (base local DB-IP, migración 0065; nunca la IP): el siguiente
    #: registro los compara (señal NETWORK_JUMP). None sin base, con una IP privada o en un validador.
    ip_country: Mapped[str | None] = mapped_column(String(2))
    ip_asn: Mapped[int | None] = mapped_column(Integer)
    #: Periodo del código de sitio con que se confirmó la presencia (antifraude 2b, migración 0070; nunca el código):
    #: el siguiente registro del empleado en ese sitio no puede reutilizar el mismo. None sin código.
    presence_window: Mapped[int | None] = mapped_column(Integer)
