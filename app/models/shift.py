"""Turnos de trabajo: sitios con geocerca, turnos, su asignación a empleados y las solicitudes de cambio.

Decisión del dueño del producto (migración 0048): **el turno tiene toda la información de dónde y
cuándo se checa**; asignar es solo elegir el turno y desde cuándo.

- `WorkSite`: lugar de trabajo de la empresa (sucursal, planta) con su domicilio, punto en el mapa y
  radio. "En sitio" es dentro de ese radio.
- `Shift`: CUÁNDO (entrada y salida en la hora del negocio, días de la semana, descansos y
  tolerancias; si la salida es igual o anterior a la entrada, termina al día siguiente) y DÓNDE (sus
  sitios, `ShiftSite`, y los días en que se puede checar remoto, `remote_weekdays`). Editarlo aplica
  desde ese momento a todos los que lo tienen asignado; lo ya registrado guarda su copia.
- `ShiftSite`: los sitios donde se checa en persona con ese turno.
- `ShiftAssignment`: qué turno tiene un empleado DESDE una fecha (y hasta otra). Un cambio de turno
  crea una asignación nueva desde mañana o después y cierra la anterior: lo ya registrado conserva el
  turno con el que ocurrió. No lleva lugar: el lugar es el de su turno.
- `ShiftChangeRequest`: el empleado pide otro turno desde una fecha; la empresa la aprueba o rechaza
  (el lugar es el del turno pedido).

Todas las relaciones con la empresa son FK compuestas `(id, company_id)`: la base impide mezclar
empresas aunque hubiera un error en el código.
"""

from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY, WORKFORCE
from app.models.mixins import AddressMixin, SoftDeleteMixin, TimestampMixin, live_unique, trash_index

#: Radio de un sitio de trabajo (m): de una oficina a un predio grande.
SITE_RADIUS_MIN_M = 10
SITE_RADIUS_MAX_M = 10_000
#: Días de la semana como bits: lunes = 1, martes = 2 ... domingo = 64 (todos = 127).
ALL_WEEKDAYS = 127
#: Sitios por turno: los que caben en una pantalla de selección (y la página más grande de la API).
MAX_SHIFT_SITES = 50
#: Descansos por turno y minutos de cada uno.
MAX_BREAKS = 6
BREAK_MINUTES_MIN = 5
BREAK_MINUTES_MAX = 240
#: Tolerancias (minutos): llegar antes, retardo, salir antes y límite para checar la salida.
TOLERANCE_MAX = 240
CHECK_OUT_WINDOW_MAX = 720


class WorkSite(SoftDeleteMixin, AddressMixin, TimestampMixin, Base):
    """Sitio de trabajo de una empresa: checar "en sitio" es hacerlo dentro de su radio. Con borrado lógico (migración
    0068): solo uno que ningún turno vigente usa y donde nadie ha checado; su nombre queda libre."""

    __tablename__ = "work_sites"
    __table_args__ = (
        # Nombre único por empresa entre los VIGENTES, sin distinguir mayúsculas; también da el orden del listado.
        live_unique("uq_work_sites_company_name", "company_id", text("lower(name)")),
        # Papelera de la empresa (el más reciente primero) y depuración de los eliminados.
        trash_index("work_sites", "company_id", "deleted_at", "id"),
        UniqueConstraint("id", "company_id", name="uq_work_sites_id_company"),
        # Un sitio siempre tiene su punto en el mapa (es lo que mide la geocerca).
        CheckConstraint(
            "latitude IS NOT NULL AND longitude IS NOT NULL AND latitude BETWEEN -90 AND 90 "
            "AND longitude BETWEEN -180 AND 180",
            name="coordinates",
        ),
        CheckConstraint(f"radius_m BETWEEN {SITE_RADIUS_MIN_M} AND {SITE_RADIUS_MAX_M}", name="radius"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    radius_m: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    #: Código de sitio (antifraude 2b, decisión D9; apagado por omisión): la entrada y la salida aquí llevan el código
    #: rotativo que muestra su kiosco (`site_codes`). Su secreto (32 bytes al azar) se guarda CIFRADO con
    #: DATA_ENCRYPTION_KEY (Fernet, texto) y nunca sale del servidor: el kiosco recibe cada código ya calculado.
    presence_code: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    presence_secret: Mapped[str | None] = mapped_column(String(255))


class Shift(SoftDeleteMixin, TimestampMixin, Base):
    """Turno de trabajo de una empresa (p. ej. "Matutino 7:00-15:00" o "Nocturno 22:00-6:00"). Con borrado lógico
    (migración 0068): solo uno que nadie tiene asignado; sus solicitudes pendientes se cancelan y su nombre queda
    libre."""

    __tablename__ = "shifts"
    __table_args__ = (
        # Nombre único por empresa entre los VIGENTES, sin distinguir mayúsculas; también da el orden del listado.
        live_unique("uq_shifts_company_name", "company_id", text("lower(name)")),
        # Papelera de la empresa (el más reciente primero) y depuración de los eliminados.
        trash_index("shifts", "company_id", "deleted_at", "id"),
        UniqueConstraint("id", "company_id", name="uq_shifts_id_company"),
        CheckConstraint("start_time <> end_time", name="times"),
        CheckConstraint(f"weekdays BETWEEN 1 AND {ALL_WEEKDAYS}", name="weekdays"),
        # Los días remotos son días del turno (un día que no trabaja no puede ser remoto).
        CheckConstraint(
            f"remote_weekdays BETWEEN 0 AND {ALL_WEEKDAYS} AND (remote_weekdays & weekdays) = remote_weekdays",
            name="remote_weekdays",
        ),
        CheckConstraint(
            f"breaks_count BETWEEN 0 AND {MAX_BREAKS} AND (breaks_count = 0 OR break_minutes BETWEEN "
            f"{BREAK_MINUTES_MIN} AND {BREAK_MINUTES_MAX})",
            name="breaks",
        ),
        CheckConstraint(
            f"early_check_in_minutes BETWEEN 0 AND {TOLERANCE_MAX} AND late_tolerance_minutes BETWEEN 0 AND "
            f"{TOLERANCE_MAX} AND early_check_out_minutes BETWEEN 0 AND {TOLERANCE_MAX} AND "
            f"late_check_out_minutes BETWEEN 0 AND {CHECK_OUT_WINDOW_MAX}",
            name="tolerances",
        ),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Hora de entrada y de salida en la hora del negocio (salida <= entrada: termina al día siguiente).
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    #: Días en que empieza el turno (bits: lunes = 1 ... domingo = 64).
    weekdays: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Días del turno en que se puede checar remoto (mismos bits); los demás, solo en sus sitios.
    remote_weekdays: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)
    breaks_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)
    #: Minutos de cada descanso.
    break_minutes: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)
    #: Cuánto antes de la entrada se puede checar.
    early_check_in_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Minutos después de la entrada que aún no cuentan como retardo.
    late_tolerance_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Minutos antes de la salida que aún no cuentan como salida anticipada.
    early_check_out_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Hasta cuántos minutos después de la salida se puede checar; después, la jornada queda sin salida.
    late_check_out_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)


class ShiftAssignment(SoftDeleteMixin, TimestampMixin, Base):
    """El turno de un empleado desde `valid_from` hasta `valid_to` (None: sin fin). Cancelar un cambio programado lo
    manda a la papelera (borrado lógico, migración 0068); restaurarlo vuelve a aplicar las reglas de asignar."""

    __tablename__ = "shift_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["employee_id", "company_id"],
            [f"{WORKFORCE}.employees.id", f"{WORKFORCE}.employees.company_id"],
            name="fk_shift_assignments_employee_company",
            ondelete="CASCADE",
        ),
        # Un turno con empleados asignados no se borra (se desactiva): su historial lo nombra.
        ForeignKeyConstraint(
            ["shift_id", "company_id"],
            [f"{WORKFORCE}.shifts.id", f"{WORKFORCE}.shifts.company_id"],
            name="fk_shift_assignments_shift_company",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "company_id", name="uq_shift_assignments_id_company"),
        # Súper-índice de las asignaciones (migración 0047): el turno vigente de un empleado en una fecha,
        # las de varios empleados (operaciones masivas), la FK del empleado y las vigentes de toda la
        # empresa (tablero) sin leer la tabla (INCLUDE valid_to, shift_id; y deleted_at desde la 0068: las
        # canceladas siguen en el índice de la FK, y la condición de las vigentes se resuelve en él).
        Index(
            "ix_shift_assignments_company_employee",
            "company_id",
            "employee_id",
            "valid_from",
            postgresql_include=["valid_to", "shift_id", "deleted_at"],
        ),
        # Depuración de los cambios cancelados (la papelera de un empleado sale del súper-índice).
        trash_index("shift_assignments", "company_id", "deleted_at", "id"),
        Index("ix_shift_assignments_shift", "shift_id"),
        Index(
            "ix_shift_assignments_created_by_id",
            "created_by_id",
            postgresql_where=text("created_by_id IS NOT NULL"),
            sqlite_where=text("created_by_id IS NOT NULL"),
        ),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="dates"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    employee_id: Mapped[int] = mapped_column(nullable=False)
    shift_id: Mapped[int] = mapped_column(nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))


class ShiftSite(Base):
    """Sitios donde se checa en persona con un turno (los días que no son remotos).

    Borrar el turno borra sus sitios (CASCADE); un sitio que algún turno usa no se borra (RESTRICT): se
    quita del turno o se desactiva."""

    __tablename__ = "shift_sites"
    __table_args__ = (
        ForeignKeyConstraint(
            ["shift_id", "company_id"],
            [f"{WORKFORCE}.shifts.id", f"{WORKFORCE}.shifts.company_id"],
            name="fk_shift_sites_shift_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["site_id", "company_id"],
            [f"{WORKFORCE}.work_sites.id", f"{WORKFORCE}.work_sites.company_id"],
            name="fk_shift_sites_site_company",
            ondelete="RESTRICT",
        ),
        # Por sitio: la revisión de la FK al borrar un sitio, "¿qué turnos lo usan?" y los empleados que
        # hoy checan en cada sitio. Los sitios de un turno salen de la llave primaria (shift_id, site_id).
        Index("ix_shift_sites_site", "site_id"),
        {"schema": WORKFORCE},
    )

    shift_id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)


class ShiftChangeRequest(TimestampMixin, Base):
    """El empleado pide otro turno desde una fecha (con al menos un día de anticipación)."""

    __tablename__ = "shift_change_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["employee_id", "company_id"],
            [f"{WORKFORCE}.employees.id", f"{WORKFORCE}.employees.company_id"],
            name="fk_shift_change_requests_employee_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["shift_id", "company_id"],
            [f"{WORKFORCE}.shifts.id", f"{WORKFORCE}.shifts.company_id"],
            name="fk_shift_change_requests_shift_company",
            ondelete="CASCADE",
        ),
        # Una sola solicitud pendiente por empleado (la base lo garantiza también entre dos a la vez).
        Index(
            "uq_shift_change_requests_pending",
            "employee_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
            sqlite_where=text("status = 'PENDING'"),
        ),
        # Bandeja de la empresa (por estado, la más reciente primero) y las del empleado.
        Index("ix_shift_change_requests_company_status", "company_id", "status", "id"),
        Index("ix_shift_change_requests_employee", "employee_id", "id"),
        Index("ix_shift_change_requests_shift", "shift_id"),
        Index(
            "ix_shift_change_requests_reviewed_by_id",
            "reviewed_by_id",
            postgresql_where=text("reviewed_by_id IS NOT NULL"),
            sqlite_where=text("reviewed_by_id IS NOT NULL"),
        ),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    employee_id: Mapped[int] = mapped_column(nullable=False)
    shift_id: Mapped[int] = mapped_column(nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(f"{CATALOG}.shift_request_statuses.code"),
        default="PENDING",
        server_default="PENDING",
        nullable=False,
    )
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(String(500))
