"""Departamentos de la empresa (rol COMPANY): responsables y empleados asignados."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, CompanyUser, DbSession, Pagination, require_screen, trash_of
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.department import (
    DepartmentAssignment,
    DepartmentCreate,
    DepartmentList,
    DepartmentRead,
    DepartmentUpdate,
)
from app.services.department_service import DepartmentService

router = APIRouter(
    prefix="/departments",
    tags=["Departamentos (COMPANY)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Rol sin permisos"},
    },
)

#: Administrar departamentos: solo su pantalla.
MANAGE = [Depends(require_screen(Screen.COMPANY_DEPARTMENTS))]
#: El listado también filtra a quién asignar un turno o registrar una ausencia (Turnos y Calendario).
LIST = [Depends(require_screen(Screen.COMPANY_DEPARTMENTS, Screen.COMPANY_SHIFTS, Screen.COMPANY_CALENDAR))]

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Departamento (o empleado) no encontrado"}
}
#: La papelera la ve solo la pantalla que elimina y restaura departamentos (no Turnos ni Calendario).
DepartmentTrash = Annotated[bool, Depends(trash_of(Screen.COMPANY_DEPARTMENTS))]


@router.get(
    "",
    response_model=ApiResponse[DepartmentList],
    summary="Departamentos de la empresa (paginado)",
    description="`deleted=true`: la papelera («Eliminados»), con quién y cuándo eliminó cada uno.",
    dependencies=LIST,
)
def list_departments(
    company: CompanyScope,
    db: DbSession,
    page: Pagination,
    deleted: DepartmentTrash,
    search: Annotated[str | None, Query(max_length=100, description="Fragmento del nombre")] = None,
) -> ApiResponse[DepartmentList]:
    result = DepartmentService(db, company).list_departments(search=search, page=page, deleted=deleted)
    return ok(result, code="DEPARTMENTS_LISTED", params={"count": result.total})


@router.post(
    "",
    dependencies=MANAGE,
    response_model=ApiResponse[DepartmentRead],
    status_code=status.HTTP_201_CREATED,
    summary="Crear un departamento",
    description="Nombre único en la empresa (sin distinguir mayúsculas): si ya existe, 409 `DEPARTMENT_NAME_TAKEN`.",
    responses={409: {"model": ErrorResponse, "description": "Nombre ya usado"}},
)
def create_department(payload: DepartmentCreate, company: CompanyScope, db: DbSession) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    return ok(service.read(service.create(payload)), code="DEPARTMENT_CREATED", status_code=201)


@router.get(
    "/{department_id}",
    response_model=ApiResponse[DepartmentRead],
    summary="Detalle (también de uno en «Eliminados», con `deleted_at`)",
    responses=NOT_FOUND,
    dependencies=MANAGE,
)
def get_department(department_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    return ok(service.read(service.get(department_id, include_deleted=True)), code="DEPARTMENT_FOUND")


@router.put(
    "/{department_id}",
    response_model=ApiResponse[DepartmentRead],
    summary="Editar",
    responses=NOT_FOUND,
    dependencies=MANAGE,
)
def update_department(
    department_id: int, payload: DepartmentUpdate, company: CompanyScope, db: DbSession
) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    return ok(service.read(service.update(department_id, payload)), code="DEPARTMENT_UPDATED")


@router.delete(
    "/{department_id}",
    dependencies=MANAGE,
    response_model=ApiResponse[None],
    summary="Eliminar un departamento sin empleados (a «Eliminados»)",
    description=(
        "Borrado lógico: su nombre queda libre y se puede restaurar durante `SOFT_DELETE_RETENTION_DAYS`. Sus "
        "responsables se retiran con él (restaurarlo no los regresa). Con empleados asignados responde 409 "
        "`DEPARTMENT_HAS_EMPLOYEES`."
    ),
    responses={
        **NOT_FOUND,
        409: {"model": ErrorResponse, "description": "`DEPARTMENT_HAS_EMPLOYEES` o `ALREADY_DELETED`"},
    },
)
def delete_department(department_id: int, company: CompanyScope, user: CompanyUser, db: DbSession) -> ApiResponse[None]:
    DepartmentService(db, company).delete(department_id, user)
    return ok(None, code="DEPARTMENT_DELETED")


@router.post(
    "/{department_id}/restore",
    dependencies=MANAGE,
    response_model=ApiResponse[DepartmentRead],
    summary="Restaurar un departamento de «Eliminados»",
    description="Su nombre debe seguir libre (409 `RESTORE_CONFLICT`).",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "`NOT_DELETED` o `RESTORE_CONFLICT`"}},
)
def restore_department(department_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    return ok(service.read(service.restore(department_id)), code="DEPARTMENT_RESTORED")


@router.post(
    "/{department_id}/employees",
    dependencies=MANAGE,
    response_model=ApiResponse[DepartmentRead],
    summary="Asignar un empleado al departamento",
    description="Cada empleado está a lo más en un departamento: si estaba en otro, cambia a este. Idempotente.",
    responses=NOT_FOUND,
)
def assign_employee(
    department_id: int, payload: DepartmentAssignment, company: CompanyScope, db: DbSession
) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    department = service.assign(department_id, payload.employee_id)
    return ok(service.read(department), code="DEPARTMENT_EMPLOYEE_ASSIGNED")


@router.delete(
    "/{department_id}/employees/{employee_id}",
    dependencies=MANAGE,
    response_model=ApiResponse[DepartmentRead],
    summary="Quitar un empleado del departamento (idempotente)",
    responses=NOT_FOUND,
)
def unassign_employee(
    department_id: int, employee_id: int, company: CompanyScope, db: DbSession
) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    department = service.unassign(department_id, employee_id)
    return ok(service.read(department), code="DEPARTMENT_EMPLOYEE_REMOVED")


@router.post(
    "/{department_id}/managers",
    dependencies=MANAGE,
    response_model=ApiResponse[DepartmentRead],
    summary="Nombrar responsable a un empleado de la empresa (idempotente)",
    responses=NOT_FOUND,
)
def add_manager(
    department_id: int, payload: DepartmentAssignment, company: CompanyScope, db: DbSession
) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    department = service.add_manager(department_id, payload.employee_id)
    return ok(service.read(department), code="DEPARTMENT_MANAGER_ADDED")


@router.delete(
    "/{department_id}/managers/{employee_id}",
    dependencies=MANAGE,
    response_model=ApiResponse[DepartmentRead],
    summary="Retirar a un responsable (idempotente)",
    responses=NOT_FOUND,
)
def remove_manager(
    department_id: int, employee_id: int, company: CompanyScope, db: DbSession
) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    department = service.remove_manager(department_id, employee_id)
    return ok(service.read(department), code="DEPARTMENT_MANAGER_REMOVED")
