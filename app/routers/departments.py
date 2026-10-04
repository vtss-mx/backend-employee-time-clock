"""Departamentos de la empresa (rol COMPANY): responsables y empleados asignados."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, DbSession, Pagination, require_screen
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
    dependencies=[Depends(require_screen(Screen.COMPANY_DEPARTMENTS))],
    prefix="/departments",
    tags=["Departamentos (COMPANY)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Rol sin permisos"},
    },
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Departamento (o empleado) no encontrado"}
}


@router.get("", response_model=ApiResponse[DepartmentList], summary="Departamentos de la empresa (paginado)")
def list_departments(
    company: CompanyScope,
    db: DbSession,
    page: Pagination,
    search: Annotated[str | None, Query(max_length=100, description="Fragmento del nombre")] = None,
) -> ApiResponse[DepartmentList]:
    result = DepartmentService(db, company).list_departments(search=search, page=page)
    return ok(result, f"{result.total} departamento(s)", code="DEPARTMENTS_LISTED")


@router.post(
    "",
    response_model=ApiResponse[DepartmentRead],
    status_code=status.HTTP_201_CREATED,
    summary="Crear un departamento",
    description="Nombre único en la empresa (sin distinguir mayúsculas): si ya existe, 409 `DEPARTMENT_NAME_TAKEN`.",
    responses={409: {"model": ErrorResponse, "description": "Nombre ya usado"}},
)
def create_department(payload: DepartmentCreate, company: CompanyScope, db: DbSession) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    return ok(service.read(service.create(payload)), "Departamento creado", code="DEPARTMENT_CREATED", status_code=201)


@router.get("/{department_id}", response_model=ApiResponse[DepartmentRead], summary="Detalle", responses=NOT_FOUND)
def get_department(department_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    return ok(service.read(service.get(department_id)), "Departamento encontrado", code="DEPARTMENT_FOUND")


@router.put("/{department_id}", response_model=ApiResponse[DepartmentRead], summary="Editar", responses=NOT_FOUND)
def update_department(
    department_id: int, payload: DepartmentUpdate, company: CompanyScope, db: DbSession
) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    return ok(
        service.read(service.update(department_id, payload)), "Departamento actualizado", code="DEPARTMENT_UPDATED"
    )


@router.delete(
    "/{department_id}",
    response_model=ApiResponse[None],
    summary="Eliminar un departamento sin empleados",
    description="Sus responsables se retiran con él. Con empleados asignados responde 409 `DEPARTMENT_HAS_EMPLOYEES`.",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "Tiene empleados asignados"}},
)
def delete_department(department_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[None]:
    DepartmentService(db, company).delete(department_id)
    return ok(None, "Departamento eliminado", code="DEPARTMENT_DELETED")


@router.post(
    "/{department_id}/employees",
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
    return ok(service.read(department), "Empleado asignado", code="DEPARTMENT_EMPLOYEE_ASSIGNED")


@router.delete(
    "/{department_id}/employees/{employee_id}",
    response_model=ApiResponse[DepartmentRead],
    summary="Quitar un empleado del departamento (idempotente)",
    responses=NOT_FOUND,
)
def unassign_employee(
    department_id: int, employee_id: int, company: CompanyScope, db: DbSession
) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    department = service.unassign(department_id, employee_id)
    return ok(service.read(department), "Empleado quitado del departamento", code="DEPARTMENT_EMPLOYEE_REMOVED")


@router.post(
    "/{department_id}/managers",
    response_model=ApiResponse[DepartmentRead],
    summary="Nombrar responsable a un empleado de la empresa (idempotente)",
    responses=NOT_FOUND,
)
def add_manager(
    department_id: int, payload: DepartmentAssignment, company: CompanyScope, db: DbSession
) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    department = service.add_manager(department_id, payload.employee_id)
    return ok(service.read(department), "Responsable agregado", code="DEPARTMENT_MANAGER_ADDED")


@router.delete(
    "/{department_id}/managers/{employee_id}",
    response_model=ApiResponse[DepartmentRead],
    summary="Retirar a un responsable (idempotente)",
    responses=NOT_FOUND,
)
def remove_manager(
    department_id: int, employee_id: int, company: CompanyScope, db: DbSession
) -> ApiResponse[DepartmentRead]:
    service = DepartmentService(db, company)
    department = service.remove_manager(department_id, employee_id)
    return ok(service.read(department), "Responsable retirado", code="DEPARTMENT_MANAGER_REMOVED")
