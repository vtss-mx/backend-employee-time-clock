"""Calendario de la empresa: festivos, días laborables especiales y qué días no trabaja cada empleado.

- Festivos: la empresa los agrega o quita; "Agregar festivos oficiales del año" agrega solo los que
  faltan (idempotente: repetirlo no duplica nada ni falla) y deja a la empresa quitar o sumar los suyos.
- Días laborables especiales: "esta persona sí trabaja este día" aunque sea festivo o esté dentro de
  su ausencia. Solo tiene sentido en un día que hoy es libre para ella y que no ha pasado.
- `days_off`: los días libres de unos empleados en un rango (tres consultas, sin importar cuántos
  sean); lo usan la asistencia (no se checa en un día libre) y el tablero ("Día libre", no "Faltó").
"""

from collections.abc import Iterable
from datetime import date, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models import CompanyHoliday, Employee, EmployeeAbsence, EmployeeWorkday, User
from app.repositories.calendar_repository import CalendarRepository
from app.repositories.employee_repository import EmployeeRepository
from app.schemas.calendar import (
    DayOffRead,
    HolidayCreate,
    HolidayList,
    HolidayRead,
    OfficialHolidaysResult,
    WorkdayCreate,
    WorkdayList,
    WorkdayRead,
)
from app.schemas.common import PageParams
from app.services.calendar_rules import DayOff, DaysOff, official_holidays
from app.services.catalog_service import Catalogs, get_catalogs
from app.services.shift_service import employee_ref

#: Lo que se le dice al empleado si el tipo de su ausencia ya no está en el catálogo en memoria.
FALLBACK_PHRASE = "Tienes día libre"
#: Hasta dónde se muestran los próximos festivos al empleado (un año).
UPCOMING_DAYS = 366
HOLIDAY_DATE_TAKEN = "Ya hay un día festivo en esa fecha"
WORKDAY_TAKEN = "Ese día ya es laborable para el empleado"


def day_off_span(absence: EmployeeAbsence, catalogs: Catalogs) -> DayOff:
    """Una ausencia aprobada como día libre, con el nombre y la frase de su tipo (catálogo)."""
    row = catalogs.get("day_off_types", absence.type_code)
    return DayOff(
        kind=absence.type_code,
        name=row["name"] if row else absence.type_code,
        starts_on=absence.starts_on,
        ends_on=absence.ends_on,
        phrase=row["phrase"] if row else FALLBACK_PHRASE,
    )


def day_off_read(day_off: DayOff | None, work_date: date) -> DayOffRead | None:
    if day_off is None:
        return None
    return DayOffRead(
        kind=day_off.kind,
        name=day_off.name,
        work_date=work_date,
        starts_on=day_off.starts_on,
        ends_on=day_off.ends_on,
    )


def _holiday_read(holiday: CompanyHoliday) -> HolidayRead:
    return HolidayRead(
        id=holiday.id,
        holiday_date=holiday.holiday_date,
        name=holiday.name,
        official=holiday.official,
        created_at=holiday.created_at,
    )


class CalendarService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = CalendarRepository(db, company_id)

    # ---------- Días libres (asistencia y tablero) ----------

    def days_off(self, employee_ids: Iterable[int], start: date, end: date) -> DaysOff:
        """Festivos, ausencias aprobadas y días laborables de esos empleados en el rango."""
        ids = list(employee_ids)
        catalogs = get_catalogs()
        absences = self.repo.approved_between(ids, start, end)
        return DaysOff(
            holidays=self.repo.holiday_names(start, end),
            absences={
                employee_id: [day_off_span(absence, catalogs) for absence in found]
                for employee_id, found in absences.items()
            },
            workdays=self.repo.workdays_between(ids, start, end),
        )

    # ---------- Festivos ----------

    def holidays(self, year: int, page: PageParams) -> HolidayList:
        items, total = self.repo.holidays(date(year, 1, 1), date(year, 12, 31), offset=page.offset, limit=page.size)
        return HolidayList.of([_holiday_read(h) for h in items], total, page)

    def upcoming_holidays(self, page: PageParams) -> HolidayList:
        """Los festivos de hoy en adelante (lo que ve el empleado en "Mis días libres")."""
        today = business_today()
        end = today + timedelta(days=UPCOMING_DAYS)
        items, total = self.repo.holidays(today, end, offset=page.offset, limit=page.size)
        return HolidayList.of([_holiday_read(h) for h in items], total, page)

    def add_holiday(self, data: HolidayCreate, actor: User) -> HolidayRead:
        if self.repo.holiday_names(data.holiday_date, data.holiday_date):
            raise ConflictError(HOLIDAY_DATE_TAKEN, code="HOLIDAY_DATE_TAKEN", field="holiday_date")
        holiday = CompanyHoliday(holiday_date=data.holiday_date, name=data.name, created_by_id=actor.id)
        try:
            self.repo.add_all([holiday])
            self.db.commit()
        except IntegrityError as exc:  # otro festivo en esa fecha creado al mismo tiempo
            self.db.rollback()
            raise ConflictError(HOLIDAY_DATE_TAKEN, code="HOLIDAY_DATE_TAKEN", field="holiday_date") from exc
        return _holiday_read(holiday)

    def delete_holiday(self, holiday_id: int) -> None:
        holiday = self.repo.holiday(holiday_id)
        if holiday is None:
            raise NotFoundError("Día festivo no encontrado", code="HOLIDAY_NOT_FOUND")
        self.repo.delete_holiday(holiday)
        self.db.commit()

    def add_official(self, year: int, actor: User) -> OfficialHolidaysResult:
        """Agrega los festivos oficiales del año que faltan (una fecha que ya es festivo, con cualquier
        nombre, se respeta). Repetirlo no duplica nada."""
        taken = self.repo.holiday_names(date(year, 1, 1), date(year, 12, 31))
        missing = [
            CompanyHoliday(holiday_date=day, name=name, official=True, created_by_id=actor.id)
            for day, name in official_holidays(year)
            if day not in taken
        ]
        self.repo.add_all(missing)
        self.db.commit()
        return OfficialHolidaysResult(
            year=year, added=[_holiday_read(h) for h in missing], existing=len(official_holidays(year)) - len(missing)
        )

    # ---------- Días laborables especiales ----------

    def workdays(
        self, *, employee_id: int | None, start: date | None, end: date | None, page: PageParams
    ) -> WorkdayList:
        items, total = self.repo.workdays(
            employee_id=employee_id, start=start, end=end, offset=page.offset, limit=page.size
        )
        return WorkdayList.of(self._workday_reads(items), total, page)

    def _workday_reads(self, workdays: list[EmployeeWorkday]) -> list[WorkdayRead]:
        employees = EmployeeRepository(self.db, self.company_id).by_ids({w.employee_id for w in workdays})
        return [
            WorkdayRead(
                id=w.id,
                employee=employee_ref(employees[w.employee_id]),
                work_date=w.work_date,
                note=w.note,
                created_at=w.created_at,
            )
            for w in workdays
        ]

    def _employee(self, employee_id: int) -> Employee:
        employee = EmployeeRepository(self.db, self.company_id).get_by_id(employee_id)
        if employee is None:
            raise NotFoundError("Empleado no encontrado", code="EMPLOYEE_NOT_FOUND")
        return employee

    def add_workday(self, data: WorkdayCreate, actor: User) -> WorkdayRead:
        """Ese día el empleado sí trabaja: solo un día libre para él (festivo o dentro de su ausencia
        aprobada) que aún no pasa."""
        employee = self._employee(data.employee_id)
        if data.work_date < business_today():
            raise UnprocessableError(
                "Elige hoy o un día futuro: un día que ya pasó no se puede volver laborable",
                code="WORKDAY_IN_PAST",
                field="work_date",
            )
        reason = self.days_off([employee.id], data.work_date, data.work_date).on(employee.id, data.work_date)
        if reason is None:
            raise UnprocessableError(
                f"El {data.work_date:%d/%m/%Y} ya es laborable para {employee.full_name}: no es festivo ni está "
                "dentro de una ausencia suya",
                code="WORKDAY_NOT_NEEDED",
                field="work_date",
            )
        workday = EmployeeWorkday(
            employee_id=employee.id, work_date=data.work_date, note=data.note, created_by_id=actor.id
        )
        try:
            self.repo.add_all([workday])
            self.db.commit()
        except IntegrityError as exc:  # el mismo día marcado al mismo tiempo
            self.db.rollback()
            raise ConflictError(WORKDAY_TAKEN, code="WORKDAY_TAKEN", field="work_date") from exc
        return self._workday_reads([workday])[0]

    def delete_workday(self, workday_id: int) -> None:
        workday = self.repo.workday(workday_id)
        if workday is None:
            raise NotFoundError("Día laborable no encontrado", code="WORKDAY_NOT_FOUND")
        self.repo.delete_workday(workday)
        self.db.commit()
