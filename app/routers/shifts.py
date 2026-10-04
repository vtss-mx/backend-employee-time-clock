"""Turnos de la empresa, su asignación a empleados y las solicitudes de cambio (solo COMPANY, pantalla "Turnos")."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, CompanyUser, DbSession, Pagination, require_screen
from app.models import Screen, ShiftRequestStatus
from app.schemas.bulk import BulkResult
from app.schemas.common import ErrorResponse
from app.schemas.shift import (
    AssignmentBulkCreate,
    AssignmentCreate,
    AssignmentList,
    AssignmentRead,
    ShiftCreate,
    ShiftList,
    ShiftRead,
    ShiftRequestApprove,
    ShiftRequestList,
    ShiftRequestRead,
    ShiftRequestReject,
    ShiftRequestSummary,
    ShiftStatusUpdate,
    ShiftUpdate,
)
from app.services.shift_request_service import ShiftRequestService
from app.services.shift_service import ShiftService

router = APIRouter(
    tags=["Turnos (COMPANY)"],
    dependencies=[Depends(require_screen(Screen.COMPANY_SHIFTS))],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)


def _not_found(what: str) -> dict[int | str, dict[str, Any]]:
    return {404: {"model": ErrorResponse, "description": f"{what} no encontrado"}}


# ---------------------------------------------------------------- turnos


@router.get("/shifts", response_model=ApiResponse[ShiftList], summary="Turnos de la empresa (paginado)")
def list_shifts(
    _: CompanyUser,
    company_id: CompanyScope,
    db: DbSession,
    page: Pagination,
    search: Annotated[str | None, Query(max_length=100)] = None,
    active: bool | None = None,
) -> ApiResponse[ShiftList]:
    result = ShiftService(db, company_id).search(search=search, active=active, page=page)
    return ok(result, f"{result.total} turno(s)", code="SHIFTS")


@router.post(
    "/shifts",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ShiftRead],
    summary="Alta de un turno (entrada, salida, días, descansos y tolerancias)",
    responses={409: {"model": ErrorResponse, "description": "Nombre ya usado"}},
)
def create_shift(body: ShiftCreate, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[ShiftRead]:
    return ok(ShiftService(db, company_id).create(body), "Turno registrado", code="SHIFT_CREATED", status_code=201)


@router.get("/shifts/{shift_id}", response_model=ApiResponse[ShiftRead], responses=_not_found("Turno"))
def get_shift(shift_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[ShiftRead]:
    return ok(ShiftService(db, company_id).read(shift_id), "Turno", code="SHIFT")


@router.put(
    "/shifts/{shift_id}",
    response_model=ApiResponse[ShiftRead],
    summary="Editar un turno (aplica a las jornadas que aún no empiezan)",
    responses=_not_found("Turno"),
)
def update_shift(
    shift_id: int, body: ShiftUpdate, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[ShiftRead]:
    return ok(ShiftService(db, company_id).update(shift_id, body), "Turno actualizado", code="SHIFT_UPDATED")


@router.patch("/shifts/{shift_id}/status", response_model=ApiResponse[ShiftRead], responses=_not_found("Turno"))
def set_shift_status(
    shift_id: int, body: ShiftStatusUpdate, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[ShiftRead]:
    shift = ShiftService(db, company_id).set_active(shift_id, body.active)
    return ok(shift, "Turno activado" if body.active else "Turno desactivado", code="SHIFT_STATUS")


@router.delete(
    "/shifts/{shift_id}",
    response_model=ApiResponse[None],
    summary="Eliminar un turno (solo si nadie lo tiene asignado)",
    responses={**_not_found("Turno"), 409: {"model": ErrorResponse, "description": "Asignado: desactívalo"}},
)
def delete_shift(shift_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[None]:
    ShiftService(db, company_id).delete(shift_id)
    return ok(None, "Turno eliminado", code="SHIFT_DELETED")


# ---------------------------------------------------------------- asignaciones


@router.get(
    "/employees/{employee_id}/shift-assignments",
    response_model=ApiResponse[AssignmentList],
    summary="Turnos del empleado (vigente, programados y anteriores)",
    responses=_not_found("Empleado"),
)
def list_assignments(
    employee_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession, page: Pagination
) -> ApiResponse[AssignmentList]:
    result = ShiftService(db, company_id).assignments(employee_id, page)
    return ok(result, f"{result.total} asignación(es)", code="SHIFT_ASSIGNMENTS")


@router.post(
    "/employees/{employee_id}/shift-assignments",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[AssignmentRead],
    summary="Asignar o cambiar el turno (con turno vigente: desde mañana o después)",
    description="Lo ya registrado conserva su turno: la asignación vigente termina el día anterior al cambio.",
    responses={**_not_found("Empleado"), 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def assign_shift(
    employee_id: int, body: AssignmentCreate, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[AssignmentRead]:
    assignment = ShiftService(db, company_id).assign(employee_id, body, user)
    return ok(assignment, "Turno asignado", code="SHIFT_ASSIGNED", status_code=201)


@router.post(
    "/shift-assignments/bulk",
    response_model=ApiResponse[BulkResult],
    summary="Asignar el mismo turno a varios empleados a la vez (hasta 500)",
    description=(
        "Las mismas reglas que asignar a uno, en una transacción. Cada empleado queda asignado (`DONE`), "
        "sin cambios si ya tenía exactamente esa asignación (`UNCHANGED`: un reintento no duplica) u omitido "
        "con su motivo (`SKIPPED`: inactivo, cambio sin un día de anticipación, cambio ya programado). Lo que "
        "es igual para todos (turno, sitios, días remotos, fecha) rechaza la petición; un empleado que no es "
        "de la empresa responde 404 y no se asigna a nadie."
    ),
    responses={**_not_found("Empleado o turno"), 422: {"model": ErrorResponse}},
)
def assign_shift_bulk(
    body: AssignmentBulkCreate, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[BulkResult]:
    result = ShiftService(db, company_id).assign_many(body, user)
    return ok(result, f"Turno asignado a {result.done} empleado(s)", code="SHIFT_BULK_ASSIGNED")


@router.delete(
    "/shift-assignments/{assignment_id}",
    response_model=ApiResponse[None],
    summary="Cancelar un cambio de turno programado (aún no empieza)",
    responses={**_not_found("Asignación"), 409: {"model": ErrorResponse, "description": "Ya empezó"}},
)
def cancel_assignment(assignment_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[None]:
    ShiftService(db, company_id).cancel(assignment_id)
    return ok(None, "Cambio de turno cancelado", code="SHIFT_ASSIGNMENT_CANCELLED")


# ---------------------------------------------------------------- solicitudes de cambio


@router.get("/shift-requests", response_model=ApiResponse[ShiftRequestList], summary="Solicitudes de cambio de turno")
def list_requests(
    _: CompanyUser,
    company_id: CompanyScope,
    db: DbSession,
    page: Pagination,
    request_status: Annotated[ShiftRequestStatus | None, Query(alias="status")] = None,
) -> ApiResponse[ShiftRequestList]:
    result = ShiftRequestService(db, company_id).inbox(status=request_status, page=page)
    return ok(result, f"{result.total} solicitud(es)", code="SHIFT_REQUESTS")


@router.get(
    "/shift-requests/summary",
    response_model=ApiResponse[ShiftRequestSummary],
    summary="Solicitudes pendientes (contador del menú)",
)
def requests_summary(_: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[ShiftRequestSummary]:
    pending = ShiftRequestService(db, company_id).pending()
    return ok(ShiftRequestSummary(pending=pending), f"{pending} pendiente(s)", code="SHIFT_REQUESTS_SUMMARY")


@router.post(
    "/shift-requests/{request_id}/approve",
    response_model=ApiResponse[ShiftRequestRead],
    summary="Aprobar: programa el nuevo turno (con al menos un día de anticipación)",
    responses={**_not_found("Solicitud"), 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def approve_request(
    request_id: int, body: ShiftRequestApprove, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[ShiftRequestRead]:
    result = ShiftRequestService(db, company_id).approve(request_id, body, user)
    return ok(result, "Cambio de turno aprobado", code="SHIFT_REQUEST_APPROVED")


@router.post(
    "/shift-requests/{request_id}/reject",
    response_model=ApiResponse[ShiftRequestRead],
    summary="Rechazar (con motivo para el empleado)",
    responses={**_not_found("Solicitud"), 409: {"model": ErrorResponse}},
)
def reject_request(
    request_id: int, body: ShiftRequestReject, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[ShiftRequestRead]:
    result = ShiftRequestService(db, company_id).reject(request_id, body, user)
    return ok(result, "Solicitud rechazada", code="SHIFT_REQUEST_REJECTED")
