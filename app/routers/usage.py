"""Consumo de la plataforma (solo rol ADMIN, pantalla "Consumo"): peticiones, datos enviados y recibidos,
tiempo de proceso, errores y almacenamiento de cada empresa y de cada usuario, en un rango de días.

Son estadísticas: en la admisión van en `BACKGROUND_PREFIXES` (al saturarse ceden su lugar a checar,
identificar e iniciar sesión).
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.responses import ApiResponse, ok
from app.dependencies import AdminUser, DbSession, Pagination, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.usage import CompanyUsage, CompanyUsageList, RouteUsageList, UsageOverview, UserUsageList
from app.services.usage_service import UsageService

router = APIRouter(
    prefix="/admin/usage",
    tags=["Consumo (ADMIN)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Solo el administrador de la plataforma"},
        422: {"model": ErrorResponse, "description": "Rango de fechas inválido o de más de un año"},
    },
    dependencies=[Depends(require_screen(Screen.ADMIN_USAGE))],
)
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Empresa no encontrada"}}
Start = Annotated[date | None, Query(description="Primer día (por omisión, el primero del mes)")]
End = Annotated[date | None, Query(description="Último día (por omisión, hoy)")]


@router.get("/overview", response_model=ApiResponse[UsageOverview], summary="Consumo de toda la plataforma")
def usage_overview(_: AdminUser, db: DbSession, start: Start = None, end: End = None) -> ApiResponse[UsageOverview]:
    return ok(UsageService(db).overview(start, end), code="USAGE_OVERVIEW")


@router.get("/companies", response_model=ApiResponse[CompanyUsageList], summary="Empresas con su consumo (paginado)")
def usage_companies(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    start: Start = None,
    end: End = None,
    search: Annotated[
        str | None, Query(max_length=100, description="Nombre, razón social o identificador fiscal")
    ] = None,
    sort: Annotated[str, Query(pattern="^(requests|bytes|duration|errors|storage)$")] = "requests",
) -> ApiResponse[CompanyUsageList]:
    result = UsageService(db).companies(start, end, search=search, sort=sort, page=page)
    return ok(result, code="USAGE_COMPANIES", params={"count": result.total})


@router.get(
    "/companies/{company_id}",
    response_model=ApiResponse[CompanyUsage],
    summary="Consumo de una empresa",
    responses=NOT_FOUND,
)
def company_usage(
    company_id: int, _: AdminUser, db: DbSession, start: Start = None, end: End = None
) -> ApiResponse[CompanyUsage]:
    return ok(UsageService(db).company(company_id, start, end), code="COMPANY_USAGE")


@router.get(
    "/companies/{company_id}/users",
    response_model=ApiResponse[UserUsageList],
    summary="Consumo de cada cuenta de una empresa (paginado)",
    responses=NOT_FOUND,
)
def company_usage_users(
    company_id: int, _: AdminUser, db: DbSession, page: Pagination, start: Start = None, end: End = None
) -> ApiResponse[UserUsageList]:
    result = UsageService(db).users(company_id, start, end, page)
    return ok(result, code="USAGE_USERS", params={"count": result.total})


@router.get(
    "/companies/{company_id}/routes",
    response_model=ApiResponse[RouteUsageList],
    summary="Consumo por ruta de la API de una empresa (paginado)",
    responses=NOT_FOUND,
)
def company_usage_routes(
    company_id: int, _: AdminUser, db: DbSession, page: Pagination, start: Start = None, end: End = None
) -> ApiResponse[RouteUsageList]:
    result = UsageService(db).routes(company_id, start, end, page)
    return ok(result, code="USAGE_ROUTES", params={"count": result.total})
