"""Calendario de la empresa: qué días no se trabaja y quién sí trabaja aunque no se trabaje.

- `CompanyHoliday`: día festivo de toda la empresa (los oficiales de México se agregan con un botón;
  la empresa agrega o quita los suyos).
- `EmployeeAbsence`: días que un empleado no trabaja (vacaciones, permiso, incapacidad...). La
  registra la empresa (aprobada de una vez) o la pide el empleado (pendiente hasta que la empresa la
  aprueba o la rechaza). Los estados son los de `shift_request_statuses` (pendiente, aprobada,
  rechazada, cancelada): una solicitud del empleado sigue el mismo ciclo que un cambio de turno.
- `EmployeeWorkday`: "esta persona SÍ trabaja este día" aunque sea festivo o esté dentro de su
  ausencia (p. ej. la empresa necesita a alguien el 25 de diciembre).

Regla (`calendar_rules.day_off_on`): un día es libre si es festivo de la empresa o lo cubre una
ausencia APROBADA, salvo que el empleado tenga ese día como laborable. En un día libre no se puede
checar la entrada.
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY, WORKFORCE
from app.core.sql_safety import sql_identifier
from app.models.mixins import SoftDeleteMixin, TimestampMixin, company_fk, live_unique, trash_index

#: Días máximos de una ausencia (ambos incluidos): un año, incluido uno bisiesto.
ABSENCE_MAX_DAYS = 366
#: Estados en que una ausencia ocupa sus días (no se pueden encimar dos así).
ACTIVE_ABSENCE = "status IN ('PENDING', 'APPROVED')"


def _employee_fk(table: str) -> ForeignKeyConstraint:
    """El empleado es de la MISMA empresa (FK compuesta): la base impide mezclar empresas."""
    return company_fk(table, "employee_id", f"{WORKFORCE}.employees")


def _by_user(table: str, column: str) -> Index:
    """Índice de la FK hacia quien registró o decidió (casi siempre con valor)."""
    return Index(
        f"ix_{table}_{column}",
        column,
        postgresql_where=text(f"{sql_identifier(column)} IS NOT NULL"),
        sqlite_where=text(f"{sql_identifier(column)} IS NOT NULL"),
    )


class CompanyHoliday(SoftDeleteMixin, TimestampMixin, Base):
    """Día festivo de la empresa: nadie checa ese día, salvo quien lo tenga como laborable. Con borrado lógico
    (migración 0068): eliminado deja de ser día libre y su fecha queda libre para otro."""

    __tablename__ = "company_holidays"
    __table_args__ = (
        # Un festivo VIGENTE por día; también da el listado por año (company_id + rango de fechas).
        live_unique("uq_company_holidays_company_date", "company_id", "holiday_date"),
        # Papelera de la empresa (el más reciente primero) y depuración de los eliminados.
        trash_index("company_holidays", "company_id", "deleted_at", "id"),
        _by_user("company_holidays", "created_by_id"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Festivo oficial (Ley Federal del Trabajo, art. 74) agregado con "Agregar festivos oficiales".
    official: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))


class EmployeeAbsence(TimestampMixin, Base):
    """Días que un empleado no trabaja (de `starts_on` a `ends_on`, ambos incluidos)."""

    __tablename__ = "employee_absences"
    __table_args__ = (
        _employee_fk("employee_absences"),
        # Las ausencias del empleado (su lista y la FK).
        Index("ix_employee_absences_employee", "employee_id", "starts_on"),
        # Las que ocupan días (pendientes o aprobadas): no encimar, días libres, tablero.
        Index(
            "ix_employee_absences_employee_active",
            "employee_id",
            "starts_on",
            "ends_on",
            postgresql_where=text(ACTIVE_ABSENCE),
            sqlite_where=text(ACTIVE_ABSENCE),
        ),
        # Listado de la empresa (por fecha), su bandeja por estado en el mismo orden del listado y el
        # contador de pendientes del menú (migración 0047: starts_on en el índice de estado).
        Index("ix_employee_absences_company_starts", "company_id", "starts_on", "id"),
        Index("ix_employee_absences_company_status", "company_id", "status", "starts_on", "id"),
        _by_user("employee_absences", "requested_by_id"),
        _by_user("employee_absences", "decided_by_id"),
        CheckConstraint(f"ends_on >= starts_on AND ends_on - starts_on < {ABSENCE_MAX_DAYS}", name="dates"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    employee_id: Mapped[int] = mapped_column(nullable=False)
    type_code: Mapped[str] = mapped_column(
        String(30), ForeignKey(f"{CATALOG}.day_off_types.code", ondelete="RESTRICT"), nullable=False
    )
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    #: Lo que explica quien la registra o la pide (p. ej. "Vacaciones de fin de año").
    note: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(f"{CATALOG}.shift_request_statuses.code", ondelete="RESTRICT"),
        default="PENDING",
        server_default="PENDING",
        nullable=False,
    )
    #: Quién la registró: la cuenta de la empresa o la del propio empleado (una solicitud).
    requested_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    #: Quién la aprobó, rechazó o canceló, cuándo y con qué motivo (el rechazo lo lleva siempre).
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(500))


class EmployeeWorkday(SoftDeleteMixin, TimestampMixin, Base):
    """El empleado SÍ trabaja ese día aunque sea festivo o esté dentro de una ausencia suya. Con borrado lógico
    (migración 0068): eliminado, ese día vuelve a ser libre para él."""

    __tablename__ = "employee_workdays"
    __table_args__ = (
        _employee_fk("employee_workdays"),
        # Uno VIGENTE por empleado y día; también da su lista por fecha.
        live_unique("uq_employee_workdays_employee_date", "employee_id", "work_date"),
        # La FK del empleado (su depuración borra sus días laborables en cascada): el único parcial no la cubre.
        Index("ix_employee_workdays_employee", "employee_id"),
        # Papelera de la empresa (el más reciente primero) y depuración de los eliminados.
        trash_index("employee_workdays", "company_id", "deleted_at", "id"),
        # Listado de la empresa y tablero de un día.
        Index("ix_employee_workdays_company_date", "company_id", "work_date", "id"),
        _by_user("employee_workdays", "created_by_id"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    employee_id: Mapped[int] = mapped_column(nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
