"""Deriva de las señales del motor facial (solo el ADMIN, pantalla «Deriva de señales»; antifraude fase 3).

Son estadísticas: las lecturas van en `BACKGROUND_PREFIXES` de la admisión. «Calcular ahora» repite el cálculo de la
última ventana completa (idempotente: lo mismo que hace el mantenimiento al cerrar cada semana).
"""

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.clock import business_today
from app.core.config import settings
from app.core.exceptions import ConflictError
from app.core.responses import ApiResponse, ok
from app.dependencies import AdminUser, DbSession, Pagination, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.drift import CompanyDriftList, DriftList, DriftPlatform, DriftStatus, DriftSummary
from app.services import drift_service
from app.services.drift_stats import last_closed_window

router = APIRouter(
    prefix="/admin/drift",
    tags=["Deriva de señales (ADMIN)"],
    dependencies=[Depends(require_screen(Screen.ADMIN_DRIFT))],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Solo el ADMIN de la plataforma"},
    },
)
Week = Annotated[date | None, Query(description="Inicio de la ventana (lunes); sin ella, la más reciente calculada")]


@router.get("/summary", response_model=ApiResponse[DriftSummary], summary="Configuración, ventanas y alertas")
def drift_summary(_: AdminUser, db: DbSession) -> ApiResponse[DriftSummary]:
    return ok(drift_service.summary(db), code="DRIFT_SUMMARY")


@router.get("", response_model=ApiResponse[DriftList], summary="Señal × plataforma de una ventana (paginado)")
def drift_signals(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    week: Week = None,
    platform: Annotated[DriftPlatform | None, Query(description="Filtrar por plataforma")] = None,
    status: Annotated[DriftStatus | None, Query(description="Filtrar por estado")] = None,
) -> ApiResponse[DriftList]:
    result = drift_service.signals_page(db, week=week, platform=platform, status=status, page=page)
    return ok(result, code="DRIFT_SIGNALS", params={"count": result.total})


@router.get(
    "/companies",
    response_model=ApiResponse[CompanyDriftList],
    summary="Empresas de una ventana: casos por intento y revisiones aprobadas sin mirar (paginado)",
)
def drift_companies(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    week: Week = None,
    search: Annotated[str | None, Query(max_length=100, description="Parte del nombre de la empresa")] = None,
) -> ApiResponse[CompanyDriftList]:
    result = drift_service.companies_page(db, week=week, search=search, page=page)
    return ok(result, code="DRIFT_COMPANIES", params={"count": result.total})


@router.post(
    "/compute",
    response_model=ApiResponse[DriftSummary],
    summary="Calcular ahora la última ventana completa (lo mismo que hace el mantenimiento; idempotente)",
    responses={409: {"model": ErrorResponse, "description": "El monitoreo de deriva está apagado"}},
)
def drift_compute(_: AdminUser, db: DbSession) -> ApiResponse[DriftSummary]:
    if not settings.DRIFT_ENABLED:
        raise ConflictError(code="DRIFT_DISABLED")
    week_start = last_closed_window(business_today(), settings.DRIFT_WINDOW_DAYS)
    rows = drift_service.compute_window(db, week_start, datetime.now(UTC))
    return ok(drift_service.summary(db), code="DRIFT_COMPUTED", params={"count": rows})
