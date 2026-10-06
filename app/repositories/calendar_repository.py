"""Consultas del calendario de días libres de UNA empresa: festivos, ausencias y días laborables.

Un id de otra empresa se comporta como inexistente (404). Saber qué días no trabaja un grupo de
empleados en un rango son tres consultas (festivos, ausencias aprobadas y días laborables), sin
importar cuántos empleados o días sean. Festivos y días laborables tienen borrado lógico: un día libre se calcula
solo con lo vigente; la papelera y restaurar ven lo eliminado (`include_deleted`).
"""

from collections.abc import Iterable
from datetime import date, datetime, timedelta

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import Session

from app.models import CompanyHoliday, EmployeeAbsence, EmployeeWorkday, ShiftRequestStatus
from app.models.calendar import ABSENCE_MAX_DAYS
from app.repositories.aggregates import affected_rows, get_scoped, insert_many, paginate, trash_page

#: Estados en que una ausencia ocupa sus días (no se encima otra): pendiente o aprobada.
ACTIVE_STATUSES = (ShiftRequestStatus.PENDING, ShiftRequestStatus.APPROVED)

type CalendarRecord = CompanyHoliday | EmployeeAbsence | EmployeeWorkday


def _started_since(start: date) -> ColumnElement[bool]:
    """Cota inferior de `starts_on` para una ausencia que llega hasta `start` o después.

    Una ausencia dura a lo más ABSENCE_MAX_DAYS días (CHECK `dates` de la tabla): si termina en `start`
    o después, empezó a lo más ABSENCE_MAX_DAYS - 1 días antes. La condición no cambia el resultado,
    pero acota el recorrido de los índices por fecha de inicio a una ventana fija: sin ella, "toca el
    rango" (`ends_on >= start AND starts_on <= end`) solo acota por arriba y la base leía años de
    ausencias anteriores."""
    return EmployeeAbsence.starts_on >= start - timedelta(days=ABSENCE_MAX_DAYS - 1)


def absence_touches(start: date, end: date) -> tuple[ColumnElement[bool], ...]:
    """La ausencia cubre al menos un día de [start, end] (con la cota de `_started_since`)."""
    return (_started_since(start), EmployeeAbsence.starts_on <= end, EmployeeAbsence.ends_on >= start)


class CalendarRepository:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def _get[T: (CompanyHoliday, EmployeeAbsence, EmployeeWorkday)](
        self, model: type[T], record_id: int, *, lock: bool = False, include_deleted: bool = False
    ) -> T | None:
        return get_scoped(self.db, model, record_id, self.company_id, lock=lock, include_deleted=include_deleted)

    def add_all(self, records: Iterable[CalendarRecord]) -> None:
        """Una inserción para todos (p. ej. los festivos oficiales o una ausencia colectiva)."""
        items = list(records)
        for record in items:
            record.company_id = self.company_id
        self.db.add_all(items)
        self.db.flush()

    def insert_absences(self, absences: list[EmployeeAbsence]) -> None:
        """Una ausencia para cada uno de varios empleados en una sola inserción (vacaciones colectivas)."""
        for absence in absences:
            absence.company_id = self.company_id
        insert_many(self.db, absences)

    # ---------- Festivos ----------

    def holiday(self, holiday_id: int, *, include_deleted: bool = False) -> CompanyHoliday | None:
        return self._get(CompanyHoliday, holiday_id, include_deleted=include_deleted)

    def _holidays_between(self, start: date, end: date) -> ColumnElement[bool]:
        return (
            (CompanyHoliday.company_id == self.company_id)
            & (CompanyHoliday.holiday_date >= start)
            & (CompanyHoliday.holiday_date <= end)
        )

    def holidays(
        self, start: date, end: date, *, offset: int, limit: int, deleted: bool = False
    ) -> tuple[list[CompanyHoliday], int]:
        """Una página de los festivos del rango, en orden de fecha (índice único empresa + fecha) o, con `deleted`, los
        del rango que están en la papelera (el eliminado más reciente primero)."""
        stmt = select(CompanyHoliday).where(self._holidays_between(start, end))
        if deleted:
            return trash_page(self.db, stmt, CompanyHoliday, offset=offset, limit=limit)
        return paginate(self.db, stmt, (CompanyHoliday.holiday_date,), offset=offset, limit=limit)

    def holiday_names(self, start: date, end: date) -> dict[date, str]:
        """Festivos del rango por fecha (un rango corto: los días de una jornada, un año a lo más)."""
        stmt = select(CompanyHoliday.holiday_date, CompanyHoliday.name).where(self._holidays_between(start, end))
        return dict(self.db.execute(stmt).all())  # filas (fecha, nombre)

    # ---------- Ausencias ----------

    def absence(self, absence_id: int, *, lock: bool = False) -> EmployeeAbsence | None:
        return self._get(EmployeeAbsence, absence_id, lock=lock)

    def absences(
        self,
        *,
        employee_id: int | None,
        type_code: str | None,
        status: str | None,
        start: date | None,
        end: date | None,
        offset: int,
        limit: int,
    ) -> tuple[list[EmployeeAbsence], int]:
        """Ausencias con filtros; con fechas, las que tocan el rango. La más reciente primero."""
        stmt = select(EmployeeAbsence).where(EmployeeAbsence.company_id == self.company_id)
        if employee_id is not None:
            stmt = stmt.where(EmployeeAbsence.employee_id == employee_id)
        if type_code is not None:
            stmt = stmt.where(EmployeeAbsence.type_code == type_code)
        if status is not None:
            stmt = stmt.where(EmployeeAbsence.status == status)
        if start is not None:
            stmt = stmt.where(EmployeeAbsence.ends_on >= start, _started_since(start))
        if end is not None:
            stmt = stmt.where(EmployeeAbsence.starts_on <= end)
        order = (EmployeeAbsence.starts_on.desc(), EmployeeAbsence.id.desc())
        return paginate(self.db, stmt, order, offset=offset, limit=limit)

    def _covering(
        self, employee_ids: list[int], start: date, end: date, statuses: Iterable[str]
    ) -> dict[int, list[EmployeeAbsence]]:
        found: dict[int, list[EmployeeAbsence]] = {employee_id: [] for employee_id in employee_ids}
        if not employee_ids:
            return found
        stmt = select(EmployeeAbsence).where(
            EmployeeAbsence.company_id == self.company_id,
            EmployeeAbsence.employee_id.in_(employee_ids),
            EmployeeAbsence.status.in_(list(statuses)),
            *absence_touches(start, end),
        )
        for absence in self.db.scalars(stmt.order_by(EmployeeAbsence.starts_on, EmployeeAbsence.id)):
            found[absence.employee_id].append(absence)
        return found

    def active_overlapping(
        self, employee_ids: Iterable[int], start: date, end: date
    ) -> dict[int, list[EmployeeAbsence]]:
        """Ausencias pendientes o aprobadas de cada empleado que tocan el rango (no se enciman)."""
        return self._covering(list(employee_ids), start, end, ACTIVE_STATUSES)

    def approved_between(self, employee_ids: Iterable[int], start: date, end: date) -> dict[int, list[EmployeeAbsence]]:
        """Ausencias aprobadas de cada empleado que tocan el rango (días libres)."""
        return self._covering(list(employee_ids), start, end, (ShiftRequestStatus.APPROVED,))

    def cancel_pending_absences(self, employee_id: int, actor_id: int, now: datetime) -> None:
        """Las solicitudes PENDIENTES de un empleado que se elimina quedan canceladas (una sentencia): no se quedan en
        la bandeja de la empresa ni en su contador."""
        stmt = update(EmployeeAbsence).where(
            EmployeeAbsence.company_id == self.company_id,
            EmployeeAbsence.employee_id == employee_id,
            EmployeeAbsence.status == ShiftRequestStatus.PENDING,
        )
        values = {"status": ShiftRequestStatus.CANCELLED, "decided_by_id": actor_id, "decided_at": now}
        affected_rows(self.db, stmt.values(**values))

    def pending_absences(self) -> int:
        stmt = select(func.count()).where(
            EmployeeAbsence.company_id == self.company_id, EmployeeAbsence.status == ShiftRequestStatus.PENDING
        )
        return int(self.db.scalar(stmt) or 0)

    # ---------- Días laborables especiales ----------

    def workday(self, workday_id: int, *, include_deleted: bool = False) -> EmployeeWorkday | None:
        return self._get(EmployeeWorkday, workday_id, include_deleted=include_deleted)

    def workdays(
        self,
        *,
        employee_id: int | None,
        start: date | None,
        end: date | None,
        offset: int,
        limit: int,
        deleted: bool = False,
    ) -> tuple[list[EmployeeWorkday], int]:
        """Los vigentes (el más reciente primero) o, con `deleted`, la papelera (el eliminado más reciente primero)."""
        stmt = select(EmployeeWorkday).where(EmployeeWorkday.company_id == self.company_id)
        if employee_id is not None:
            stmt = stmt.where(EmployeeWorkday.employee_id == employee_id)
        if start is not None:
            stmt = stmt.where(EmployeeWorkday.work_date >= start)
        if end is not None:
            stmt = stmt.where(EmployeeWorkday.work_date <= end)
        if deleted:
            return trash_page(self.db, stmt, EmployeeWorkday, offset=offset, limit=limit)
        order = (EmployeeWorkday.work_date.desc(), EmployeeWorkday.id.desc())
        return paginate(self.db, stmt, order, offset=offset, limit=limit)

    def workdays_between(self, employee_ids: Iterable[int], start: date, end: date) -> set[tuple[int, date]]:
        """(empleado, fecha) que la empresa marcó como laborables en el rango."""
        ids = list(employee_ids)
        if not ids:
            return set()
        stmt = select(EmployeeWorkday.employee_id, EmployeeWorkday.work_date).where(
            EmployeeWorkday.company_id == self.company_id,
            EmployeeWorkday.employee_id.in_(ids),
            EmployeeWorkday.work_date >= start,
            EmployeeWorkday.work_date <= end,
        )
        return {(employee_id, day) for employee_id, day in self.db.execute(stmt)}
