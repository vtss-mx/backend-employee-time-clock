"""Asistencia por turno.

- `company_router` (COMPANY, pantalla "Asistencia"): el tablero del día, el historial, la evidencia
  de cada jornada y registrar o corregir la jornada de un empleado (solo la empresa: sin rostro ni
  ubicación, con su motivo).
- `employee_router` (EMPLOYEE, pantalla "Mi asistencia"): qué puede registrar ahora, cada registro
  (con su rostro, prueba de vida y su ubicación), su historial, los turnos disponibles y sus
  solicitudes de cambio de turno.
"""

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status

from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CameraLabel,
    CompanyScope,
    CompanyUser,
    DbSession,
    EmployeeUser,
    Liveness,
    Pagination,
    Pipeline,
    read_image_uploads,
    request_meta,
    require_screen,
    verification_rate_limit,
)
from app.models import AttendanceAction, Screen, VerificationLog, WorkSessionStatus
from app.schemas.attendance import (
    AttendanceActionResult,
    AttendanceBoard,
    AttendanceHistory,
    AttendanceToday,
    CompanySessionDetail,
    CompanySessionList,
    ManualSessionCreate,
    ManualSessionUpdate,
)
from app.schemas.auth import DeviceLocation
from app.schemas.common import ErrorResponse
from app.schemas.shift import ShiftList, ShiftRequestCreate, ShiftRequestList, ShiftRequestRead
from app.schemas.verification import VerificationResult
from app.services.attendance_manual import ManualAttendance
from app.services.attendance_overview import AttendanceOverview
from app.services.attendance_service import AttendanceService
from app.services.employee_access import approved_employee
from app.services.shift_request_service import ShiftRequestService
from app.services.shift_service import ShiftService
from app.services.verification_service import VerificationService

RESPONSES: dict[int | str, dict[str, Any]] = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}}

company_router = APIRouter(
    prefix="/attendance",
    tags=["Asistencia (COMPANY)"],
    dependencies=[Depends(require_screen(Screen.COMPANY_ATTENDANCE))],
    responses=RESPONSES,
)
employee_router = APIRouter(
    prefix="/me",
    tags=["Mi asistencia (EMPLOYEE)"],
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_ATTENDANCE))],
    responses=RESPONSES,
)

#: Cada acción en la URL.
ACTIONS: dict[str, AttendanceAction] = {
    "check-in": AttendanceAction.CHECK_IN,
    "break-start": AttendanceAction.BREAK_START,
    "break-end": AttendanceAction.BREAK_END,
    "check-out": AttendanceAction.CHECK_OUT,
}


# ---------------------------------------------------------------- empresa


@company_router.get(
    "/board",
    response_model=ApiResponse[AttendanceBoard],
    summary="Tablero de un día: quién está en turno, en descanso, salió o falta",
)
def board(
    _: CompanyUser,
    company_id: CompanyScope,
    db: DbSession,
    page: Pagination,
    work_date: Annotated[date | None, Query(alias="date", description="Por omisión, hoy")] = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> ApiResponse[AttendanceBoard]:
    result = AttendanceOverview(db, company_id).board(work_date, search=search, page=page)
    return ok(result, f"{result.total} empleado(s) con turno", code="ATTENDANCE_BOARD")


@company_router.get(
    "/sessions", response_model=ApiResponse[CompanySessionList], summary="Historial de jornadas (paginado)"
)
def sessions(
    _: CompanyUser,
    company_id: CompanyScope,
    db: DbSession,
    page: Pagination,
    employee_id: Annotated[int | None, Query(gt=0)] = None,
    start: date | None = None,
    end: date | None = None,
    session_status: Annotated[WorkSessionStatus | None, Query(alias="status")] = None,
) -> ApiResponse[CompanySessionList]:
    result = AttendanceOverview(db, company_id).history(
        employee_id=employee_id, start=start, end=end, status=session_status, page=page
    )
    return ok(result, f"{result.total} jornada(s)", code="WORK_SESSIONS")


@company_router.get(
    "/sessions/{session_id}",
    response_model=ApiResponse[CompanySessionDetail],
    summary="Una jornada con la evidencia de cada registro",
    responses={404: {"model": ErrorResponse}},
)
def session_detail(
    session_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[CompanySessionDetail]:
    return ok(AttendanceOverview(db, company_id).detail(session_id), "Jornada", code="WORK_SESSION")


@company_router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[CompanySessionDetail],
    summary="Registrar la jornada de un empleado que no checó (solo la empresa)",
    description=(
        "Entrada, salida opcional y descansos en la hora del negocio (`HH:MM`), con motivo obligatorio. Sin "
        "rostro ni ubicación: queda como «Registrado por la empresa». Las mismas reglas que en vivo (ventanas del "
        "turno, nada en el futuro, descansos dentro del horario y a lo más los del turno). 409 `DAY_OFF` en un día "
        "libre (márcalo como laborable en Calendario) y `ATTENDANCE_SESSION_EXISTS` si ya tiene su jornada."
    ),
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def create_session(
    body: ManualSessionCreate, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[CompanySessionDetail]:
    result = ManualAttendance(db, company_id).create(body, user)
    return ok(result, "Asistencia registrada", code="ATTENDANCE_SESSION_CREATED", status_code=201)


@company_router.put(
    "/sessions/{session_id}",
    response_model=ApiResponse[CompanySessionDetail],
    summary="Corregir las horas y los descansos de una jornada (solo la empresa)",
    description="Lo que había queda en la bitácora; la corrección se suma con su motivo y quién la hizo.",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def correct_session(
    session_id: int, body: ManualSessionUpdate, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[CompanySessionDetail]:
    result = ManualAttendance(db, company_id).correct(session_id, body, user)
    return ok(result, "Asistencia corregida", code="ATTENDANCE_SESSION_CORRECTED")


# ---------------------------------------------------------------- empleado


@employee_router.get(
    "/attendance/today",
    response_model=ApiResponse[AttendanceToday],
    summary="Qué puedo registrar ahora (lo decide el servidor)",
)
def today(user: EmployeeUser, db: DbSession) -> ApiResponse[AttendanceToday]:
    employee = approved_employee(user)
    result = AttendanceService(db, employee.company_id).today(employee)
    return ok(result, result.message, code="ATTENDANCE_TODAY")


@employee_router.post(
    "/attendance/{action}",
    response_model=ApiResponse[AttendanceActionResult],
    summary="Registrar entrada, descanso o salida (rostro + ubicación)",
    description=(
        "Multipart: `images` (capturas frontales), `challenge_id` + `challenge_image` + `flash_image` "
        "(prueba de vida), "
        "`latitude`, `longitude` y `accuracy` (la del navegador). La hora es la del servidor. Sin un rostro "
        "verificado no se registra nada (200 con `verified: false`)."
    ),
    dependencies=[Depends(verification_rate_limit)],
    responses={409: {"model": ErrorResponse}, 413: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def record(
    request: Request,
    action: Literal["check-in", "break-start", "break-end", "check-out"],
    user: EmployeeUser,
    db: DbSession,
    pipeline: Pipeline,
    images: Annotated[list[UploadFile], File(description="Capturas frontales (JPEG/PNG/WEBP)")],
    latitude: Annotated[float, Form(ge=-90, le=90)],
    longitude: Annotated[float, Form(ge=-180, le=180)],
    accuracy: Annotated[float, Form(ge=0, le=100_000, description="Precisión (m) que informa el navegador")],
    liveness: Liveness,
    camera_label: CameraLabel = None,
) -> ApiResponse[AttendanceActionResult]:
    employee = approved_employee(user)
    frontal = read_image_uploads(images, max_files=3)
    ip, user_agent = request_meta(request)
    verifier = VerificationService(db, ip=ip, user_agent=user_agent)

    def verify() -> tuple[VerificationResult, VerificationLog | None]:
        result = verifier.verify_face(user, frontal, pipeline, liveness=liveness, camera_label=camera_label)
        return result, verifier.last_log

    location = DeviceLocation(latitude=latitude, longitude=longitude, accuracy=accuracy)
    result = AttendanceService(db, employee.company_id).act(employee, user, ACTIONS[action], location, verify)
    code = "ATTENDANCE_RECORDED" if result.verified else "IDENTITY_NOT_VERIFIED"
    return ok(result, result.message, code=code)


@employee_router.get(
    "/attendance/history", response_model=ApiResponse[AttendanceHistory], summary="Mis jornadas (paginado)"
)
def history(user: EmployeeUser, db: DbSession, page: Pagination) -> ApiResponse[AttendanceHistory]:
    employee = approved_employee(user)
    result = AttendanceOverview(db, employee.company_id).mine(employee, page)
    return ok(result, f"{result.total} jornada(s)", code="MY_WORK_SESSIONS")


@employee_router.get(
    "/shifts", response_model=ApiResponse[ShiftList], summary="Turnos activos de mi empresa (para pedir un cambio)"
)
def available_shifts(user: EmployeeUser, db: DbSession, page: Pagination) -> ApiResponse[ShiftList]:
    employee = approved_employee(user)
    result = ShiftService(db, employee.company_id).search(search=None, active=True, page=page)
    return ok(result, f"{result.total} turno(s)", code="SHIFTS")


@employee_router.get(
    "/shift-requests", response_model=ApiResponse[ShiftRequestList], summary="Mis solicitudes de cambio de turno"
)
def my_requests(user: EmployeeUser, db: DbSession, page: Pagination) -> ApiResponse[ShiftRequestList]:
    employee = approved_employee(user)
    result = ShiftRequestService(db, employee.company_id).mine(employee, page)
    return ok(result, f"{result.total} solicitud(es)", code="SHIFT_REQUESTS")


@employee_router.post(
    "/shift-requests",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ShiftRequestRead],
    summary="Pedir un cambio de turno (con al menos un día de anticipación)",
    responses={409: {"model": ErrorResponse, "description": "Ya hay una pendiente"}, 422: {"model": ErrorResponse}},
)
def request_change(body: ShiftRequestCreate, user: EmployeeUser, db: DbSession) -> ApiResponse[ShiftRequestRead]:
    employee = approved_employee(user)
    result = ShiftRequestService(db, employee.company_id).create(employee, body)
    return ok(result, "Solicitud enviada a tu empresa", code="SHIFT_REQUEST_CREATED", status_code=201)


@employee_router.post(
    "/shift-requests/{request_id}/cancel",
    response_model=ApiResponse[ShiftRequestRead],
    summary="Cancelar mi solicitud pendiente",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def cancel_request(request_id: int, user: EmployeeUser, db: DbSession) -> ApiResponse[ShiftRequestRead]:
    employee = approved_employee(user)
    result = ShiftRequestService(db, employee.company_id).cancel(employee, request_id)
    return ok(result, "Solicitud cancelada", code="SHIFT_REQUEST_CANCELLED")
