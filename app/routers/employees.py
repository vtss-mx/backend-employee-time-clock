"""Administración de empleados. Todos los endpoints requieren rol COMPANY."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, DbSession, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeList,
    EmployeeRead,
    EmployeeStatusUpdate,
    EmployeeUpdate,
    IdentityReverifyRequest,
)
from app.schemas.qr import EmployeeQrRead
from app.schemas.verification import VerificationLogRead
from app.services.employee_service import EmployeeService
from app.services.qr_service import QrService

router = APIRouter(
    prefix="/employees",
    tags=["Empleados (COMPANY)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Rol sin permisos"},
    },
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Empleado no encontrado"}}


@router.get(
    "",
    response_model=ApiResponse[EmployeeList],
    summary="Listar/buscar empleados",
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES, Screen.COMPANY_DASHBOARD))],
)
def list_employees(
    company: CompanyScope,
    db: DbSession,
    search: Annotated[str | None, Query(max_length=100, description="Nombre, número o correo")] = None,
    active: Annotated[bool | None, Query(description="Filtrar por estado")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[EmployeeList]:
    result = EmployeeService(db, company).list_employees(search=search, active=active, page=page, size=size)
    return ok(result, f"{result.total} empleado(s) encontrado(s)", code="EMPLOYEES_LISTED")


@router.post(
    "",
    response_model=ApiResponse[EmployeeRead],
    status_code=status.HTTP_201_CREATED,
    summary="Registrar empleado (solo datos)",
    description=(
        "Crea el usuario EMPLOYEE, el empleado y genera automáticamente su QR. "
        "El rostro NO se registra aquí: el empleado lo registra en su primer inicio de "
        "sesión (`face_status=NOT_ENROLLED`) y después COMPANY valida su identidad en "
        "`/api/enrollments`."
    ),
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def create_employee(payload: EmployeeCreate, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    return ok(
        service.read(service.create(payload)),
        "Empleado registrado correctamente",
        code="EMPLOYEE_CREATED",
        status_code=status.HTTP_201_CREATED,
    )


@router.get(
    "/{employee_id}",
    response_model=ApiResponse[EmployeeRead],
    summary="Detalle de empleado",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def get_employee(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    return ok(service.read(service.get(employee_id)), "Empleado encontrado", code="EMPLOYEE_FOUND")


@router.put(
    "/{employee_id}",
    response_model=ApiResponse[EmployeeRead],
    summary="Editar empleado (solo campos enviados)",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def update_employee(
    employee_id: int, payload: EmployeeUpdate, company: CompanyScope, db: DbSession
) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    return ok(service.read(service.update(employee_id, payload)), "Empleado actualizado", code="EMPLOYEE_UPDATED")


@router.patch(
    "/{employee_id}/status",
    response_model=ApiResponse[EmployeeRead],
    summary="Activar / desactivar empleado",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def set_employee_status(
    employee_id: int, payload: EmployeeStatusUpdate, company: CompanyScope, db: DbSession
) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    employee = service.read(service.set_active(employee_id, payload.active))
    message = "Empleado activado" if payload.active else "Empleado desactivado"
    return ok(employee, message, code="EMPLOYEE_ACTIVATED" if payload.active else "EMPLOYEE_DEACTIVATED")


@router.delete(
    "/{employee_id}",
    response_model=ApiResponse[None],
    summary="Eliminar empleado definitivamente (incluye datos biométricos y QR)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def delete_employee(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[None]:
    EmployeeService(db, company).delete(employee_id)
    return ok(None, "Empleado eliminado definitivamente", code="EMPLOYEE_DELETED")


# ---------------- Rostro ----------------


@router.post(
    "/{employee_id}/face/reset",
    response_model=ApiResponse[EmployeeRead],
    summary="Solicitar nueva verificación de identidad",
    description=(
        "Elimina los datos faciales del empleado y lo regresa a `NOT_ENROLLED`: en su próximo "
        "acceso deberá registrar su rostro con prueba de vida y COMPANY validarlo otra vez. "
        "`reason` (opcional) se le muestra al empleado."
    ),
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def reset_face(
    employee_id: int, company: CompanyScope, db: DbSession, payload: IdentityReverifyRequest | None = None
) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    employee = service.read(service.reset_face(employee_id, payload.reason if payload else None))
    return ok(
        employee,
        "Se solicitó al empleado verificar nuevamente su identidad.",
        code="IDENTITY_REVERIFY_REQUESTED",
    )


# ---------------- QR ----------------


@router.get(
    "/{employee_id}/qr",
    response_model=ApiResponse[EmployeeQrRead],
    summary="Obtener QR activo",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def get_qr(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeQrRead]:
    employee = EmployeeService(db, company).get(employee_id)
    qr_service = QrService(db)
    return ok(qr_service.to_read(employee, qr_service.get_active(employee)), "QR activo", code="QR_FOUND")


@router.post(
    "/{employee_id}/qr/regenerate",
    response_model=ApiResponse[EmployeeQrRead],
    summary="Regenerar QR (invalida el anterior)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def regenerate_qr(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeQrRead]:
    employee = EmployeeService(db, company).get(employee_id)
    qr_service = QrService(db)
    qr = qr_service.issue(employee)
    db.commit()
    db.refresh(qr)
    return ok(qr_service.to_read(employee, qr), "QR regenerado. El anterior ya no es válido.", code="QR_REGENERATED")


@router.delete(
    "/{employee_id}/qr",
    response_model=ApiResponse[None],
    summary="Revocar QR",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def revoke_qr(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[None]:
    employee = EmployeeService(db, company).get(employee_id)
    QrService(db).revoke(employee)
    db.commit()
    return ok(None, "QR revocado", code="QR_REVOKED")


# ---------------- Historial ----------------


@router.get(
    "/{employee_id}/verifications",
    response_model=ApiResponse[list[VerificationLogRead]],
    summary="Últimos intentos de verificación",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def verification_history(
    employee_id: int,
    company: CompanyScope,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[list[VerificationLogRead]]:
    logs = EmployeeService(db, company).history(employee_id, limit)
    return ok(logs, f"{len(logs)} intento(s) de verificación", code="VERIFICATIONS_LISTED")
