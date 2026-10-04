"""Sitios de trabajo de la empresa (solo COMPANY, pantalla "Sitios de trabajo"): dónde se checa en sitio."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, CompanyUser, DbSession, Pagination, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.shift import SiteCreate, SiteList, SiteRead, SiteStatusUpdate, SiteUpdate
from app.services.site_service import SiteService

router = APIRouter(
    prefix="/sites",
    tags=["Sitios de trabajo (COMPANY)"],
    dependencies=[Depends(require_screen(Screen.COMPANY_SITES))],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Sitio no encontrado"}}


@router.get("", response_model=ApiResponse[SiteList], summary="Sitios de la empresa (paginado)")
def list_sites(
    _: CompanyUser,
    company_id: CompanyScope,
    db: DbSession,
    page: Pagination,
    search: Annotated[str | None, Query(max_length=100)] = None,
    active: bool | None = None,
) -> ApiResponse[SiteList]:
    result = SiteService(db, company_id).search(search=search, active=active, page=page)
    return ok(result, f"{result.total} sitio(s)", code="SITES")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[SiteRead],
    summary="Alta de un sitio (domicilio con su punto en el mapa y radio)",
    responses={409: {"model": ErrorResponse, "description": "Nombre ya usado"}},
)
def create_site(body: SiteCreate, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[SiteRead]:
    return ok(SiteService(db, company_id).create(body), "Sitio registrado", code="SITE_CREATED", status_code=201)


@router.get("/{site_id}", response_model=ApiResponse[SiteRead], summary="Un sitio", responses=NOT_FOUND)
def get_site(site_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[SiteRead]:
    return ok(SiteService(db, company_id).read(site_id), "Sitio", code="SITE")


@router.put("/{site_id}", response_model=ApiResponse[SiteRead], summary="Editar un sitio", responses=NOT_FOUND)
def update_site(
    site_id: int, body: SiteUpdate, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[SiteRead]:
    return ok(SiteService(db, company_id).update(site_id, body), "Sitio actualizado", code="SITE_UPDATED")


@router.patch(
    "/{site_id}/status", response_model=ApiResponse[SiteRead], summary="Activar / desactivar", responses=NOT_FOUND
)
def set_site_status(
    site_id: int, body: SiteStatusUpdate, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[SiteRead]:
    site = SiteService(db, company_id).set_active(site_id, body.active)
    return ok(site, "Sitio activado" if body.active else "Sitio desactivado", code="SITE_STATUS")


@router.delete(
    "/{site_id}",
    response_model=ApiResponse[None],
    summary="Eliminar un sitio (solo si ninguna asignación lo usa)",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "En uso: desactívalo"}},
)
def delete_site(site_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[None]:
    SiteService(db, company_id).delete(site_id)
    return ok(None, "Sitio eliminado", code="SITE_DELETED")
