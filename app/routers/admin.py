"""Consola de la plataforma: alta y administración de empresas. Solo rol ADMIN.

El ADMIN no ve empleados ni datos biométricos de las empresas (privacidad por diseño): solo sus
datos fiscales y de contacto, sus administradores y conteos.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import AdminUser, DbSession
from app.schemas.common import ErrorResponse
from app.schemas.company import (
    CompanyAdminCreate,
    CompanyAdminPasswordReset,
    CompanyCreate,
    CompanyDetail,
    CompanyList,
    CompanyStatusUpdate,
    CompanyUpdate,
    PlatformStats,
)
from app.schemas.employee import EmployeeStatusUpdate
from app.services.company_service import CompanyService

router = APIRouter(
    prefix="/admin",
    tags=["Plataforma (ADMIN)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Solo el administrador de la plataforma"},
    },
)
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Empresa no encontrada"}}
CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorResponse, "description": "RFC o correo ya registrados"}
}


@router.get("/stats", response_model=ApiResponse[PlatformStats], summary="Indicadores de la plataforma")
def platform_stats(_: AdminUser, db: DbSession) -> ApiResponse[PlatformStats]:
    return ok(CompanyService(db).stats(), "Indicadores de la plataforma", code="PLATFORM_STATS")


@router.get("/companies", response_model=ApiResponse[CompanyList], summary="Listar/buscar empresas")
def list_companies(
    _: AdminUser,
    db: DbSession,
    search: Annotated[str | None, Query(max_length=100, description="Nombre, razón social o RFC")] = None,
    active: Annotated[bool | None, Query(description="Filtrar por estado")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[CompanyList]:
    result = CompanyService(db).list_companies(search=search, active=active, page=page, size=size)
    return ok(result, f"{result.total} empresa(s) encontrada(s)", code="COMPANIES_LISTED")


@router.get(
    "/companies/availability",
    response_model=ApiResponse[dict],
    summary="Validar en tiempo real RFC de empresa o correo de administrador",
)
def company_availability(
    _: AdminUser,
    db: DbSession,
    field: Annotated[Literal["rfc", "admin_email"], Query()],
    value: Annotated[str, Query(max_length=255)] = "",
    exclude_id: Annotated[int | None, Query(description="Al editar: id de la empresa")] = None,
) -> ApiResponse[dict]:
    result = CompanyService(db).availability(field, value, exclude_id)
    return ok(result.as_dict(), result.message, code=result.code)


@router.post(
    "/companies",
    response_model=ApiResponse[CompanyDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Dar de alta una empresa con su primer administrador",
    responses=CONFLICT,
)
def create_company(payload: CompanyCreate, _: AdminUser, db: DbSession) -> ApiResponse[CompanyDetail]:
    return ok(CompanyService(db).create(payload), "Empresa registrada", code="COMPANY_CREATED", status_code=201)


@router.get(
    "/companies/{company_id}", response_model=ApiResponse[CompanyDetail], summary="Detalle", responses=NOT_FOUND
)
def get_company(company_id: int, _: AdminUser, db: DbSession) -> ApiResponse[CompanyDetail]:
    return ok(CompanyService(db).detail(company_id), "Empresa encontrada", code="COMPANY_FOUND")


@router.put(
    "/companies/{company_id}",
    response_model=ApiResponse[CompanyDetail],
    summary="Editar datos de la empresa (solo los campos enviados)",
    responses={**NOT_FOUND, **CONFLICT},
)
def update_company(company_id: int, payload: CompanyUpdate, _: AdminUser, db: DbSession) -> ApiResponse[CompanyDetail]:
    return ok(CompanyService(db).update(company_id, payload), "Empresa actualizada", code="COMPANY_UPDATED")


@router.patch(
    "/companies/{company_id}/status",
    response_model=ApiResponse[CompanyDetail],
    summary="Activar o desactivar (desactivar cierra las sesiones de todo su personal)",
    responses=NOT_FOUND,
)
def set_company_status(
    company_id: int, payload: CompanyStatusUpdate, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).set_active(company_id, payload.active)
    return ok(detail, "Empresa activada" if payload.active else "Empresa desactivada", code="COMPANY_STATUS_UPDATED")


@router.post(
    "/companies/{company_id}/admins",
    response_model=ApiResponse[CompanyDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Agregar un administrador a la empresa",
    responses={**NOT_FOUND, **CONFLICT},
)
def add_company_admin(
    company_id: int, payload: CompanyAdminCreate, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).add_admin(company_id, payload)
    return ok(detail, "Administrador agregado", code="COMPANY_ADMIN_CREATED", status_code=201)


@router.patch(
    "/companies/{company_id}/admins/{user_id}/status",
    response_model=ApiResponse[CompanyDetail],
    summary="Activar o desactivar un administrador de la empresa",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "Último administrador activo"}},
)
def set_company_admin_status(
    company_id: int, user_id: int, payload: EmployeeStatusUpdate, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).set_admin_active(company_id, user_id, payload.active)
    return ok(detail, "Administrador actualizado", code="COMPANY_ADMIN_STATUS_UPDATED")


@router.put(
    "/companies/{company_id}/admins/{user_id}/password",
    response_model=ApiResponse[CompanyDetail],
    summary="Restablecer la contraseña de un administrador de la empresa (contraseña olvidada)",
    description="Asigna una contraseña nueva y cierra de inmediato las sesiones abiertas de ese administrador.",
    responses=NOT_FOUND,
)
def reset_company_admin_password(
    company_id: int, user_id: int, payload: CompanyAdminPasswordReset, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).reset_admin_password(company_id, user_id, payload)
    return ok(detail, "Contraseña restablecida", code="COMPANY_ADMIN_PASSWORD_RESET")
