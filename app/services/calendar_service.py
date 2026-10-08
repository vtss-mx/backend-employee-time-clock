"""Calendario de la empresa: festivos, días laborables especiales y qué días no trabaja cada empleado.

- Festivos: la empresa los agrega o quita; "Agregar festivos oficiales del año" agrega solo los que
  faltan (idempotente: repetirlo no duplica nada ni falla) y deja a la empresa quitar o sumar los suyos.
- Días laborables especiales: "esta persona sí trabaja este día" aunque sea festivo o esté dentro de
  su ausencia. Solo tiene sentido en un día que hoy es libre para ella y que no ha pasado.
- `days_off`: los días libres de unos empleados en un rango (tres consultas, sin importar cuántos
  sean); lo usan la asistencia (no se checa en un día libre) y el tablero ("Día libre", no "Faltó").
- Eliminar un festivo o un día laborable es un borrado lógico (regla 20 de la raíz): deja de contar para los días
  libres y su fecha queda libre; restaurarlo revisa de nuevo que nadie la haya ocupado (y, el día laborable, las
  mismas reglas que al marcarlo).
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.i18n import LazyText, t
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
from app.schemas.common import PageParams, deletion_of
from app.services.calendar_rules import DayOff, DaysOff, official_holidays
from app.services.catalog_service import Catalogs, get_catalogs
from app.services.shift_service import employee_ref
from app.services.trash import commit_restore, ensure_deleted, ensure_live

#: Hasta dónde se muestran los próximos festivos al empleado (un año).
UPCOMING_DAYS = 366


def day_off_phrase(type_code: str) -> LazyText:
    """La frase de un tipo de ausencia («Estás de vacaciones»), diferida: se lee del catálogo en el idioma del mensaje
    que la lleva (sin el tipo en el catálogo, la genérica)."""

    def phrase() -> str:
        row = get_catalogs().get("day_off_types", type_code)
        return str(row["phrase"]) if row else t("DAY_OFF_FALLBACK_PHRASE")

    return phrase


def day_off_span(absence: EmployeeAbsence, catalogs: Catalogs) -> DayOff:
    """Una ausencia aprobada como día libre, con el nombre y la frase de su tipo (catálogo)."""
    row = catalogs.get("day_off_types", absence.type_code)
    return DayOff(
        kind=absence.type_code,
        name=row["name"] if row else absence.type_code,
        starts_on=absence.starts_on,
        ends_on=absence.ends_on,
        phrase=day_off_phrase(absence.type_code),
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
        **deletion_of(holiday),
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

    def holidays(self, year: int, page: PageParams, *, deleted: bool = False) -> HolidayList:
        """Los festivos vigentes del año o, con `deleted`, los del año que están en «Eliminados»."""
        items, total = self.repo.holidays(
            date(year, 1, 1), date(year, 12, 31), offset=page.offset, limit=page.size, deleted=deleted
        )
        return HolidayList.of([_holiday_read(h) for h in items], total, page)

    def upcoming_holidays(self, page: PageParams) -> HolidayList:
        """Los festivos de hoy en adelante (lo que ve el empleado en "Mis días libres")."""
        today = business_today()
        end = today + timedelta(days=UPCOMING_DAYS)
        items, total = self.repo.holidays(today, end, offset=page.offset, limit=page.size)
        return HolidayList.of([_holiday_read(h) for h in items], total, page)

    def add_holiday(self, data: HolidayCreate, actor: User) -> HolidayRead:
        if self.repo.holiday_names(data.holiday_date, data.holiday_date):
            raise ConflictError(code="HOLIDAY_DATE_TAKEN", field="holiday_date")
        holiday = CompanyHoliday(holiday_date=data.holiday_date, name=data.name, created_by_id=actor.id)
        try:
            self.repo.add_all([holiday])
            self.db.commit()
        except IntegrityError as exc:  # otro festivo en esa fecha creado al mismo tiempo
            self.db.rollback()
            raise ConflictError(code="HOLIDAY_DATE_TAKEN", field="holiday_date") from exc
        return _holiday_read(holiday)

    def _holiday(self, holiday_id: int) -> CompanyHoliday:
        """Vigente o en «Eliminados»: 404 solo si no existe o es de otra empresa."""
        holiday = self.repo.holiday(holiday_id, include_deleted=True)
        if holiday is None:
            raise NotFoundError(code="HOLIDAY_NOT_FOUND")
        return holiday

    def delete_holiday(self, holiday_id: int, actor: User) -> None:
        """A «Eliminados»: ese día deja de ser libre y su fecha queda libre para otro festivo."""
        holiday = self._holiday(holiday_id)
        ensure_live(holiday)
        holiday.mark_deleted(datetime.now(UTC), actor.email)
        self.db.commit()

    def restore_holiday(self, holiday_id: int) -> HolidayRead:
        """Regresa de «Eliminados» si ningún festivo vigente ocupó su fecha (409 `RESTORE_CONFLICT`)."""
        holiday = self._holiday(holiday_id)
        ensure_deleted(holiday)
        if self.repo.holiday_names(holiday.holiday_date, holiday.holiday_date):
            raise ConflictError(
                code="RESTORE_CONFLICT",
                key="RESTORE_HOLIDAY_DATE_TAKEN",
                params={"date": holiday.holiday_date},
                field="holiday_date",
            )
        holiday.mark_restored()
        commit_restore(self.db)
        return _holiday_read(holiday)

    def add_official(self, year: int, actor: User) -> OfficialHolidaysResult:
        """Agrega los festivos oficiales del año que faltan (una fecha que ya es festivo, con cualquier
        nombre, se respeta). Repetirlo no duplica nada."""
        taken = self.repo.holiday_names(date(year, 1, 1), date(year, 12, 31))
        missing = [
            # El nombre se guarda en el idioma de quien los agrega: desde ahí es un dato de la empresa (lo edita).
            CompanyHoliday(holiday_date=day, name=t(key), official=True, created_by_id=actor.id)
            for day, key in official_holidays(year)
            if day not in taken
        ]
        self.repo.add_all(missing)
        self.db.commit()
        return OfficialHolidaysResult(
            year=year, added=[_holiday_read(h) for h in missing], existing=len(official_holidays(year)) - len(missing)
        )

    # ---------- Días laborables especiales ----------

    def workdays(
        self,
        *,
        employee_id: int | None,
        start: date | None,
        end: date | None,
        page: PageParams,
        deleted: bool = False,
    ) -> WorkdayList:
        """Los vigentes o, con `deleted`, los que están en «Eliminados» (el más reciente primero)."""
        items, total = self.repo.workdays(
            employee_id=employee_id, start=start, end=end, offset=page.offset, limit=page.size, deleted=deleted
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
                **deletion_of(w),
            )
            for w in workdays
        ]

    def _employee(self, employee_id: int) -> Employee:
        employee = EmployeeRepository(self.db, self.company_id).get_by_id(employee_id)
        if employee is None:
            raise NotFoundError(code="EMPLOYEE_NOT_FOUND")
        return employee

    def _ensure_workday_allowed(self, employee: Employee, work_date: date) -> None:
        """Un día laborable especial solo en un día libre para el empleado (festivo o dentro de su ausencia aprobada)
        que aún no pasa: al marcarlo y al restaurarlo."""
        if work_date < business_today():
            raise UnprocessableError(
                code="WORKDAY_IN_PAST",
                field="work_date",
            )
        reason = self.days_off([employee.id], work_date, work_date).on(employee.id, work_date)
        if reason is None:
            raise UnprocessableError(
                code="WORKDAY_NOT_NEEDED",
                params={"date": work_date, "name": employee.full_name},
                field="work_date",
            )

    def add_workday(self, data: WorkdayCreate, actor: User) -> WorkdayRead:
        """Ese día el empleado sí trabaja: solo un día libre para él (festivo o dentro de su ausencia
        aprobada) que aún no pasa."""
        employee = self._employee(data.employee_id)
        self._ensure_workday_allowed(employee, data.work_date)
        workday = EmployeeWorkday(
            employee_id=employee.id, work_date=data.work_date, note=data.note, created_by_id=actor.id
        )
        try:
            self.repo.add_all([workday])
            self.db.commit()
        except IntegrityError as exc:  # el mismo día marcado al mismo tiempo
            self.db.rollback()
            raise ConflictError(code="WORKDAY_TAKEN", field="work_date") from exc
        return self._workday_reads([workday])[0]

    def _workday(self, workday_id: int) -> EmployeeWorkday:
        """Vigente o en «Eliminados»: 404 solo si no existe o es de otra empresa."""
        workday = self.repo.workday(workday_id, include_deleted=True)
        if workday is None:
            raise NotFoundError(code="WORKDAY_NOT_FOUND")
        return workday

    def delete_workday(self, workday_id: int, actor: User) -> None:
        """A «Eliminados»: ese día vuelve a ser libre para el empleado."""
        workday = self._workday(workday_id)
        ensure_live(workday)
        workday.mark_deleted(datetime.now(UTC), actor.email)
        self.db.commit()

    def restore_workday(self, workday_id: int) -> WorkdayRead:
        """Regresa de «Eliminados» con las MISMAS reglas que al marcarlo (el empleado vigente, un día libre que aún no
        pasa) y si ese día no se marcó de nuevo (409 `RESTORE_CONFLICT`)."""
        workday = self._workday(workday_id)
        ensure_deleted(workday)
        employee = EmployeeRepository(self.db, self.company_id).get_by_id(workday.employee_id)
        if employee is None:
            raise ConflictError(code="RESTORE_CONFLICT", key="RESTORE_EMPLOYEE_DELETED", field="employee_id")
        day = workday.work_date
        if self.repo.workdays_between([employee.id], day, day):  # ese día ya se marcó otra vez
            raise ConflictError(code="RESTORE_CONFLICT", key="RESTORE_WORKDAY_TAKEN", field="work_date")
        self._ensure_workday_allowed(employee, day)
        workday.mark_restored()
        commit_restore(self.db)
        return self._workday_reads([workday])[0]
