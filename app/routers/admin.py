"""Consola de la plataforma: alta y administración de empresas. Solo rol ADMIN.

El ADMIN no ve empleados ni datos biométricos de las empresas (privacidad por diseño): solo sus
datos fiscales y de contacto, sus administradores y conteos.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import AdminUser, DbSession, Pagination, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.company import (
    CompanyAdminCreate,
    CompanyAdminList,
    CompanyAdminPasswordReset,
    CompanyAdminRead,
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


@router.get(
    "/stats",
    response_model=ApiResponse[PlatformStats],
    summary="Indicadores de la plataforma",
    dependencies=[Depends(require_screen(Screen.ADMIN_DASHBOARD))],
)
def platform_stats(_: AdminUser, db: DbSession) -> ApiResponse[PlatformStats]:
    return ok(CompanyService(db).stats(), "Indicadores de la plataforma", code="PLATFORM_STATS")


@router.get(
    "/companies",
    response_model=ApiResponse[CompanyList],
    summary="Listar/buscar empresas",
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES, Screen.ADMIN_DASHBOARD))],
)
def list_companies(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    search: Annotated[str | None, Query(max_length=100, description="Nombre, razón social o RFC")] = None,
    active: Annotated[bool | None, Query(description="Filtrar por estado")] = None,
) -> ApiResponse[CompanyList]:
    result = CompanyService(db).list_companies(search=search, active=active, page=page)
    return ok(result, f"{result.total} empresa(s) encontrada(s)", code="COMPANIES_LISTED")


@router.post(
    "/companies",
    response_model=ApiResponse[CompanyDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Dar de alta una empresa con su primer administrador",
    responses=CONFLICT,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def create_company(payload: CompanyCreate, _: AdminUser, db: DbSession) -> ApiResponse[CompanyDetail]:
    return ok(CompanyService(db).create(payload), "Empresa registrada", code="COMPANY_CREATED", status_code=201)


@router.get(
    "/companies/{company_id}",
    response_model=ApiResponse[CompanyDetail],
    summary="Detalle",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def get_company(company_id: int, _: AdminUser, db: DbSession) -> ApiResponse[CompanyDetail]:
    return ok(CompanyService(db).detail(company_id), "Empresa encontrada", code="COMPANY_FOUND")


@router.put(
    "/companies/{company_id}",
    response_model=ApiResponse[CompanyDetail],
    summary="Editar datos de la empresa (solo los campos enviados)",
    responses={**NOT_FOUND, **CONFLICT},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def update_company(company_id: int, payload: CompanyUpdate, _: AdminUser, db: DbSession) -> ApiResponse[CompanyDetail]:
    return ok(CompanyService(db).update(company_id, payload), "Empresa actualizada", code="COMPANY_UPDATED")


@router.patch(
    "/companies/{company_id}/status",
    response_model=ApiResponse[CompanyDetail],
    summary="Activar o desactivar (desactivar cierra las sesiones de todo su personal)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def set_company_status(
    company_id: int, payload: CompanyStatusUpdate, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).set_active(company_id, payload.active)
    return ok(detail, "Empresa activada" if payload.active else "Empresa desactivada", code="COMPANY_STATUS_UPDATED")


@router.delete(
    "/companies/{company_id}",
    response_model=ApiResponse[None],
    summary="Eliminar una empresa sin empleados (con empleados se desactiva)",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "La empresa tiene empleados"}},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def delete_company(company_id: int, _: AdminUser, db: DbSession) -> ApiResponse[None]:
    CompanyService(db).delete(company_id)
    return ok(None, "Empresa eliminada", code="COMPANY_DELETED")


@router.get(
    "/companies/{company_id}/admins",
    response_model=ApiResponse[CompanyAdminList],
    summary="Administradores de la empresa (paginados)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def list_company_admins(
    company_id: int, _: AdminUser, db: DbSession, page: Pagination
) -> ApiResponse[CompanyAdminList]:
    result = CompanyService(db).list_admins(company_id, page)
    return ok(result, f"{result.total} administrador(es)", code="COMPANY_ADMINS_LISTED")


@router.get(
    "/companies/{company_id}/admins/{user_id}",
    response_model=ApiResponse[CompanyAdminRead],
    summary="Un administrador de la empresa",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def get_company_admin(company_id: int, user_id: int, _: AdminUser, db: DbSession) -> ApiResponse[CompanyAdminRead]:
    return ok(CompanyService(db).admin(company_id, user_id), "Administrador encontrado", code="COMPANY_ADMIN_FOUND")


@router.post(
    "/companies/{company_id}/admins",
    response_model=ApiResponse[CompanyDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Agregar un administrador a la empresa",
    responses={**NOT_FOUND, **CONFLICT},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
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
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
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
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def reset_company_admin_password(
    company_id: int, user_id: int, payload: CompanyAdminPasswordReset, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).reset_admin_password(company_id, user_id, payload)
    return ok(detail, "Contraseña restablecida", code="COMPANY_ADMIN_PASSWORD_RESET")
