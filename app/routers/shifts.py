"""Turnos de la empresa, su asignación a empleados y las solicitudes de cambio (solo COMPANY, pantalla "Turnos")."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, CompanyUser, DbSession, Pagination, Trash, require_screen
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


def _restore(what: str) -> dict[int | str, dict[str, Any]]:
    """Restaurar (borrado lógico): 404, 409 `NOT_DELETED` / `RESTORE_CONFLICT` y las reglas que se revisan de nuevo."""
    return {
        **_not_found(what),
        409: {"model": ErrorResponse, "description": "`NOT_DELETED`, `RESTORE_CONFLICT` u otra regla"},
        422: {"model": ErrorResponse, "description": "Una regla que ya no se cumple (p. ej. la fecha ya pasó)"},
    }


# ---------------------------------------------------------------- turnos


@router.get(
    "/shifts",
    response_model=ApiResponse[ShiftList],
    summary="Turnos de la empresa (paginado)",
    description="`deleted=true`: la papelera («Eliminados»), con quién y cuándo eliminó cada uno.",
)
def list_shifts(
    _: CompanyUser,
    company_id: CompanyScope,
    db: DbSession,
    page: Pagination,
    search: Annotated[str | None, Query(max_length=100)] = None,
    active: bool | None = None,
    deleted: Trash = False,
) -> ApiResponse[ShiftList]:
    result = ShiftService(db, company_id).search(search=search, active=active, page=page, deleted=deleted)
    return ok(result, code="SHIFTS", params={"count": result.total})


@router.post(
    "/shifts",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ShiftRead],
    summary="Alta de un turno: cuándo (entrada, salida, días, descansos y tolerancias) y dónde (sitios y días remotos)",
    description=(
        "El turno dice dónde y cuándo se checa: `site_ids` (sitios activos de la empresa) y `remote_weekdays` "
        "(solo días del turno). Si algún día no es remoto necesita al menos un sitio (422 `SITE_REQUIRED`)."
    ),
    responses={409: {"model": ErrorResponse, "description": "Nombre ya usado"}, 422: {"model": ErrorResponse}},
)
def create_shift(body: ShiftCreate, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[ShiftRead]:
    return ok(ShiftService(db, company_id).create(body), code="SHIFT_CREATED", status_code=201)


@router.get(
    "/shifts/{shift_id}",
    response_model=ApiResponse[ShiftRead],
    summary="Un turno (también uno en «Eliminados», con `deleted_at`)",
    responses=_not_found("Turno"),
)
def get_shift(shift_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[ShiftRead]:
    return ok(ShiftService(db, company_id).read(shift_id, include_deleted=True), code="SHIFT")


@router.put(
    "/shifts/{shift_id}",
    response_model=ApiResponse[ShiftRead],
    summary="Editar un turno con sus sitios y días remotos (aplica desde ahora a todos los que lo tienen)",
    description="Aplica a las jornadas que aún no empiezan de quienes lo tienen asignado; lo registrado no cambia.",
    responses={**_not_found("Turno"), 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def update_shift(
    shift_id: int, body: ShiftUpdate, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[ShiftRead]:
    return ok(ShiftService(db, company_id).update(shift_id, body), code="SHIFT_UPDATED")


@router.patch("/shifts/{shift_id}/status", response_model=ApiResponse[ShiftRead], responses=_not_found("Turno"))
def set_shift_status(
    shift_id: int, body: ShiftStatusUpdate, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[ShiftRead]:
    shift = ShiftService(db, company_id).set_active(shift_id, body.active)
    return ok(shift, code="SHIFT_STATUS", key="SHIFT_ACTIVATED" if body.active else "SHIFT_DEACTIVATED")


@router.delete(
    "/shifts/{shift_id}",
    response_model=ApiResponse[None],
    summary="Eliminar un turno (a «Eliminados»; solo si nadie lo tiene asignado)",
    description=(
        "Borrado lógico: su nombre queda libre, sus solicitudes de cambio pendientes se cancelan y se puede restaurar "
        "durante `SOFT_DELETE_RETENTION_DAYS`."
    ),
    responses={
        **_not_found("Turno"),
        409: {"model": ErrorResponse, "description": "`SHIFT_IN_USE` (desactívalo) o `ALREADY_DELETED`"},
    },
)
def delete_shift(shift_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[None]:
    ShiftService(db, company_id).delete(shift_id, user)
    return ok(None, code="SHIFT_DELETED")


@router.post(
    "/shifts/{shift_id}/restore",
    response_model=ApiResponse[ShiftRead],
    summary="Restaurar un turno (su nombre libre y sus sitios vigentes)",
    responses=_restore("Turno"),
)
def restore_shift(shift_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[ShiftRead]:
    return ok(ShiftService(db, company_id).restore(shift_id), code="SHIFT_RESTORED")


# ---------------------------------------------------------------- asignaciones


@router.get(
    "/employees/{employee_id}/shift-assignments",
    response_model=ApiResponse[AssignmentList],
    summary="Turnos del empleado (vigente, programados y anteriores)",
    description="`deleted=true`: sus cambios de turno cancelados («Eliminados»), con quién y cuándo los canceló.",
    responses=_not_found("Empleado"),
)
def list_assignments(
    employee_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession, page: Pagination, deleted: Trash = False
) -> ApiResponse[AssignmentList]:
    result = ShiftService(db, company_id).assignments(employee_id, page, deleted=deleted)
    return ok(result, code="SHIFT_ASSIGNMENTS", params={"count": result.total})


@router.post(
    "/employees/{employee_id}/shift-assignments",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[AssignmentRead],
    summary="Asignar o cambiar el turno (con turno vigente: desde mañana o después)",
    description=(
        "Solo el turno y desde cuándo: dónde checa lo dice el turno. Lo ya registrado conserva su turno: la "
        "asignación vigente termina el día anterior al cambio."
    ),
    responses={**_not_found("Empleado"), 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def assign_shift(
    employee_id: int, body: AssignmentCreate, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[AssignmentRead]:
    assignment = ShiftService(db, company_id).assign(employee_id, body, user)
    return ok(assignment, code="SHIFT_ASSIGNED", status_code=201)


@router.post(
    "/shift-assignments/bulk",
    response_model=ApiResponse[BulkResult],
    summary="Asignar el mismo turno a varios empleados a la vez (hasta 500)",
    description=(
        "Las mismas reglas que asignar a uno, en una transacción. Cada empleado queda asignado (`DONE`), "
        "sin cambios si ya tenía exactamente esa asignación (`UNCHANGED`: un reintento no duplica) u omitido "
        "con su motivo (`SKIPPED`: inactivo, cambio sin un día de anticipación, cambio ya programado). Lo que "
        "es igual para todos (turno activo, fecha) rechaza la petición; un empleado que no es de la empresa "
        "responde 404 y no se asigna a nadie. Dónde checan lo dice el turno."
    ),
    responses={**_not_found("Empleado o turno"), 422: {"model": ErrorResponse}},
)
def assign_shift_bulk(
    body: AssignmentBulkCreate, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[BulkResult]:
    result = ShiftService(db, company_id).assign_many(body, user)
    return ok(result, code="SHIFT_BULK_ASSIGNED", params={"count": result.done})


@router.delete(
    "/shift-assignments/{assignment_id}",
    response_model=ApiResponse[None],
    summary="Cancelar un cambio de turno programado (aún no empieza; va a «Eliminados»)",
    responses={
        **_not_found("Asignación"),
        409: {"model": ErrorResponse, "description": "`ASSIGNMENT_STARTED` (ya empezó) o `ALREADY_DELETED`"},
    },
)
def cancel_assignment(
    assignment_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[None]:
    ShiftService(db, company_id).cancel(assignment_id, user)
    return ok(None, code="SHIFT_ASSIGNMENT_CANCELLED")


@router.post(
    "/shift-assignments/{assignment_id}/restore",
    response_model=ApiResponse[AssignmentRead],
    summary="Restaurar un cambio de turno cancelado (con las mismas reglas que asignarlo)",
    responses=_restore("Asignación"),
)
def restore_assignment(
    assignment_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[AssignmentRead]:
    return ok(ShiftService(db, company_id).restore_assignment(assignment_id), code="SHIFT_ASSIGNMENT_RESTORED")


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
    return ok(result, code="SHIFT_REQUESTS", params={"count": result.total})


@router.get(
    "/shift-requests/summary",
    response_model=ApiResponse[ShiftRequestSummary],
    summary="Solicitudes pendientes (contador del menú)",
)
def requests_summary(_: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[ShiftRequestSummary]:
    pending = ShiftRequestService(db, company_id).pending()
    return ok(ShiftRequestSummary(pending=pending), code="SHIFT_REQUESTS_SUMMARY", params={"count": pending})


@router.post(
    "/shift-requests/{request_id}/approve",
    response_model=ApiResponse[ShiftRequestRead],
    summary="Aprobar: programa el turno pedido (con al menos un día de anticipación; el lugar es el del turno)",
    responses={**_not_found("Solicitud"), 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def approve_request(
    request_id: int, body: ShiftRequestApprove, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[ShiftRequestRead]:
    result = ShiftRequestService(db, company_id).approve(request_id, body, user)
    return ok(result, code="SHIFT_REQUEST_APPROVED")


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
    return ok(result, code="SHIFT_REQUEST_REJECTED")
