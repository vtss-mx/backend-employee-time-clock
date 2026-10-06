"""Calendario de días libres.

- `company_router` (COMPANY, pantalla "Calendario"): días festivos por año (y los oficiales con un
  botón), ausencias de uno o varios empleados (vacaciones, permisos, incapacidades), las solicitudes
  de los empleados por aprobar o rechazar y los días laborables especiales.
- `employee_router` (EMPLOYEE, pantalla "Mi asistencia"): mis ausencias, los próximos festivos de mi
  empresa, pedir vacaciones o un permiso y cancelar mi solicitud pendiente.
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, CompanyUser, DbSession, EmployeeUser, Pagination, Trash, require_screen
from app.models import Screen, ShiftRequestStatus
from app.schemas.bulk import BulkResult
from app.schemas.calendar import (
    AbsenceCreate,
    AbsenceList,
    AbsenceRead,
    AbsenceReject,
    AbsenceRequest,
    AbsenceSummary,
    HolidayCreate,
    HolidayList,
    HolidayRead,
    OfficialHolidaysResult,
    WorkdayCreate,
    WorkdayList,
    WorkdayRead,
)
from app.schemas.common import ErrorResponse
from app.services.absence_service import AbsenceService
from app.services.calendar_service import CalendarService
from app.services.employee_access import approved_employee

RESPONSES: dict[int | str, dict[str, Any]] = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}}
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse}}
CLOSED: dict[int | str, dict[str, Any]] = {**NOT_FOUND, 409: {"model": ErrorResponse, "description": "Ya atendida"}}
#: Eliminar (a «Eliminados») y restaurar: 409 si ya está (o no está) en la papelera o si ya hay otro ese día.
TRASH: dict[int | str, dict[str, Any]] = {
    **NOT_FOUND,
    409: {"model": ErrorResponse, "description": "`ALREADY_DELETED`, `NOT_DELETED` o `RESTORE_CONFLICT`"},
    422: {"model": ErrorResponse, "description": "Al restaurar: una regla que ya no se cumple"},
}

company_router = APIRouter(
    prefix="/calendar",
    tags=["Calendario (COMPANY)"],
    dependencies=[Depends(require_screen(Screen.COMPANY_CALENDAR))],
    responses=RESPONSES,
)
employee_router = APIRouter(
    prefix="/me",
    tags=["Mis días libres (EMPLOYEE)"],
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_ATTENDANCE))],
    responses=RESPONSES,
)

Year = Annotated[int, Query(ge=2000, le=2100, description="Año del calendario")]


# ---------------------------------------------------------------- festivos


@company_router.get(
    "/holidays",
    response_model=ApiResponse[HolidayList],
    summary="Días festivos de un año",
    description="`deleted=true`: los del año que están en «Eliminados», con quién y cuándo los eliminó.",
)
def list_holidays(
    _: CompanyUser, company_id: CompanyScope, db: DbSession, page: Pagination, year: Year, deleted: Trash = False
) -> ApiResponse[HolidayList]:
    result = CalendarService(db, company_id).holidays(year, page, deleted=deleted)
    return ok(result, code="HOLIDAYS", params={"count": result.total})


@company_router.post(
    "/holidays",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[HolidayRead],
    summary="Agregar un día festivo de la empresa",
    responses={409: {"model": ErrorResponse, "description": "Ya hay un festivo ese día"}},
)
def create_holiday(
    body: HolidayCreate, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[HolidayRead]:
    result = CalendarService(db, company_id).add_holiday(body, user)
    return ok(result, code="HOLIDAY_CREATED", status_code=201)


@company_router.post(
    "/holidays/official",
    response_model=ApiResponse[OfficialHolidaysResult],
    summary="Agregar los festivos oficiales del año (Ley Federal del Trabajo, art. 74)",
    description="Idempotente: agrega solo los que faltan; una fecha que ya es festivo se respeta.",
)
def add_official_holidays(
    user: CompanyUser, company_id: CompanyScope, db: DbSession, year: Year
) -> ApiResponse[OfficialHolidaysResult]:
    result = CalendarService(db, company_id).add_official(year, user)
    return ok(result, code="OFFICIAL_HOLIDAYS_ADDED", params={"count": len(result.added)})


@company_router.delete(
    "/holidays/{holiday_id}",
    response_model=ApiResponse[None],
    summary="Eliminar un día festivo (a «Eliminados»: deja de ser día libre y su fecha queda libre)",
    responses=TRASH,
)
def delete_holiday(holiday_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[None]:
    CalendarService(db, company_id).delete_holiday(holiday_id, user)
    return ok(None, code="HOLIDAY_DELETED")


@company_router.post(
    "/holidays/{holiday_id}/restore",
    response_model=ApiResponse[HolidayRead],
    summary="Restaurar un día festivo (si nadie más ocupó su fecha)",
    responses=TRASH,
)
def restore_holiday(
    holiday_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[HolidayRead]:
    return ok(CalendarService(db, company_id).restore_holiday(holiday_id), code="HOLIDAY_RESTORED")


# ---------------------------------------------------------------- ausencias


@company_router.get("/absences", response_model=ApiResponse[AbsenceList], summary="Ausencias (con filtros)")
def list_absences(
    _: CompanyUser,
    company_id: CompanyScope,
    db: DbSession,
    page: Pagination,
    employee_id: Annotated[int | None, Query(gt=0)] = None,
    absence_type: Annotated[str | None, Query(alias="type", max_length=30)] = None,
    absence_status: Annotated[ShiftRequestStatus | None, Query(alias="status")] = None,
    start: Annotated[date | None, Query(description="Las que tocan el rango desde esta fecha")] = None,
    end: Annotated[date | None, Query(description="... hasta esta fecha")] = None,
) -> ApiResponse[AbsenceList]:
    result = AbsenceService(db, company_id).search(
        employee_id=employee_id, type_code=absence_type, status=absence_status, start=start, end=end, page=page
    )
    return ok(result, code="ABSENCES", params={"count": result.total})


@company_router.post(
    "/absences",
    response_model=ApiResponse[BulkResult],
    summary="Registrar una ausencia para uno o varios empleados (aprobada de una vez)",
    description=(
        "Vacaciones colectivas incluidas. Cada empleado queda con la ausencia (`DONE`), sin cambios si ya "
        "tenía exactamente esa (`UNCHANGED`) u omitido con su motivo (`SKIPPED`: inactivo o se encima con otra). "
        "Un empleado que no es de la empresa responde 404 y no se registra nada."
    ),
    responses={**NOT_FOUND, 422: {"model": ErrorResponse}},
)
def create_absences(
    body: AbsenceCreate, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[BulkResult]:
    result = AbsenceService(db, company_id).create_many(body, user)
    return ok(result, code="ABSENCES_CREATED", params={"count": result.done})


@company_router.get(
    "/absences/summary",
    response_model=ApiResponse[AbsenceSummary],
    summary="Solicitudes de vacaciones o permisos pendientes (contador del menú)",
)
def absences_summary(_: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[AbsenceSummary]:
    pending = AbsenceService(db, company_id).pending()
    return ok(AbsenceSummary(pending=pending), code="ABSENCES_SUMMARY", params={"count": pending})


@company_router.post(
    "/absences/{absence_id}/approve",
    response_model=ApiResponse[AbsenceRead],
    summary="Aprobar una solicitud: sus días quedan libres",
    responses=CLOSED,
)
def approve_absence(
    absence_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[AbsenceRead]:
    result = AbsenceService(db, company_id).approve(absence_id, user)
    return ok(result, code="ABSENCE_APPROVED")


@company_router.post(
    "/absences/{absence_id}/reject",
    response_model=ApiResponse[AbsenceRead],
    summary="Rechazar una solicitud (con motivo para el empleado)",
    responses=CLOSED,
)
def reject_absence(
    absence_id: int, body: AbsenceReject, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[AbsenceRead]:
    result = AbsenceService(db, company_id).reject(absence_id, body, user)
    return ok(result, code="ABSENCE_REJECTED")


@company_router.post(
    "/absences/{absence_id}/cancel",
    response_model=ApiResponse[AbsenceRead],
    summary="Retirar una ausencia pendiente o aprobada: sus días vuelven a ser laborables",
    responses=CLOSED,
)
def cancel_absence(
    absence_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[AbsenceRead]:
    result = AbsenceService(db, company_id).cancel(absence_id, user)
    return ok(result, code="ABSENCE_CANCELLED")


# ---------------------------------------------------------------- días laborables especiales


@company_router.get(
    "/workdays",
    response_model=ApiResponse[WorkdayList],
    summary="Días laborables especiales (con filtros)",
    description="`deleted=true`: los que están en «Eliminados», con quién y cuándo los eliminó.",
)
def list_workdays(
    _: CompanyUser,
    company_id: CompanyScope,
    db: DbSession,
    page: Pagination,
    employee_id: Annotated[int | None, Query(gt=0)] = None,
    start: date | None = None,
    end: date | None = None,
    deleted: Trash = False,
) -> ApiResponse[WorkdayList]:
    result = CalendarService(db, company_id).workdays(
        employee_id=employee_id, start=start, end=end, page=page, deleted=deleted
    )
    return ok(result, code="WORKDAYS", params={"count": result.total})


@company_router.post(
    "/workdays",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[WorkdayRead],
    summary="Marcar un día libre como laborable para un empleado (festivo o dentro de su ausencia)",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def create_workday(
    body: WorkdayCreate, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[WorkdayRead]:
    result = CalendarService(db, company_id).add_workday(body, user)
    return ok(result, code="WORKDAY_CREATED", status_code=201)


@company_router.delete(
    "/workdays/{workday_id}",
    response_model=ApiResponse[None],
    summary="Eliminar un día laborable especial (a «Eliminados»: ese día vuelve a ser libre para el empleado)",
    responses=TRASH,
)
def delete_workday(workday_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[None]:
    CalendarService(db, company_id).delete_workday(workday_id, user)
    return ok(None, code="WORKDAY_DELETED")


@company_router.post(
    "/workdays/{workday_id}/restore",
    response_model=ApiResponse[WorkdayRead],
    summary="Restaurar un día laborable especial (con las mismas reglas que marcarlo)",
    responses=TRASH,
)
def restore_workday(
    workday_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[WorkdayRead]:
    return ok(CalendarService(db, company_id).restore_workday(workday_id), code="WORKDAY_RESTORED")


# ---------------------------------------------------------------- empleado


@employee_router.get("/absences", response_model=ApiResponse[AbsenceList], summary="Mis ausencias y solicitudes")
def my_absences(user: EmployeeUser, db: DbSession, page: Pagination) -> ApiResponse[AbsenceList]:
    employee = approved_employee(user)
    result = AbsenceService(db, employee.company_id).mine(employee, page)
    return ok(result, code="MY_ABSENCES", params={"count": result.total})


@employee_router.post(
    "/absences",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[AbsenceRead],
    summary="Pedir vacaciones o un permiso (queda pendiente de la empresa)",
    responses={409: {"model": ErrorResponse, "description": "Se encima con otra"}, 422: {"model": ErrorResponse}},
)
def request_absence(body: AbsenceRequest, user: EmployeeUser, db: DbSession) -> ApiResponse[AbsenceRead]:
    employee = approved_employee(user)
    result = AbsenceService(db, employee.company_id).request(employee, body, user)
    return ok(result, code="ABSENCE_REQUESTED", status_code=201)


@employee_router.post(
    "/absences/{absence_id}/cancel",
    response_model=ApiResponse[AbsenceRead],
    summary="Cancelar mi solicitud pendiente",
    responses=CLOSED,
)
def cancel_my_absence(absence_id: int, user: EmployeeUser, db: DbSession) -> ApiResponse[AbsenceRead]:
    employee = approved_employee(user)
    result = AbsenceService(db, employee.company_id).cancel_mine(employee, absence_id)
    return ok(result, code="ABSENCE_CANCELLED", key="ABSENCE_REQUEST_CANCELLED")


@employee_router.get(
    "/holidays", response_model=ApiResponse[HolidayList], summary="Próximos días festivos de mi empresa"
)
def my_holidays(user: EmployeeUser, db: DbSession, page: Pagination) -> ApiResponse[HolidayList]:
    employee = approved_employee(user)
    result = CalendarService(db, employee.company_id).upcoming_holidays(page)
    return ok(result, code="MY_HOLIDAYS", params={"count": result.total})
