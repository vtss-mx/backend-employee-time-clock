"""Rendimiento de la plataforma (solo el ADMIN, pantalla "Rendimiento"): APIs, funciones clave, base de datos,
pantallas de la aplicación web y las alertas de peticiones lentas (regla 18).

Son estadísticas: las lecturas van en `BACKGROUND_PREFIXES` de la admisión (al saturarse ceden su lugar a checar,
identificar e iniciar sesión). Cambiar el seguimiento de una alerta es una escritura normal.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.responses import ApiResponse, ok
from app.dependencies import AdminUser, DbSession, Pagination, require_screen
from app.models import Screen, SlowAlertStatus
from app.schemas.common import ErrorResponse
from app.schemas.performance import (
    MetricKind,
    MetricList,
    MetricSeries,
    MetricSort,
    PerformanceOverview,
    PerfPeriod,
    SlowAlertDetail,
    SlowAlertList,
    SlowAlertStatusUpdate,
    SlowAlertSummary,
    StatementList,
    StatementSort,
    WebVitalsList,
)
from app.services.performance_service import PerformanceService

router = APIRouter(
    prefix="/admin/performance",
    tags=["Rendimiento (ADMIN)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Solo el administrador de la plataforma"},
        422: {"model": ErrorResponse, "description": "Periodo, tipo u orden inválido"},
    },
    dependencies=[Depends(require_screen(Screen.ADMIN_PERFORMANCE))],
)
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Alerta no encontrada"}}
Period = Annotated[PerfPeriod, Query(description="Periodo que termina ahora (1h, 6h, 24h, 7d, 30d o 90d)")]
Search = Annotated[str | None, Query(max_length=100, description="Parte del nombre (sin distinguir mayúsculas)")]


@router.get("/overview", response_model=ApiResponse[PerformanceOverview], summary="Resumen del rendimiento")
def performance_overview(_: AdminUser, db: DbSession, period: Period = "24h") -> ApiResponse[PerformanceOverview]:
    return ok(PerformanceService(db).overview(period), code="PERFORMANCE_OVERVIEW")


@router.get(
    "/metrics",
    response_model=ApiResponse[MetricList],
    summary="Rutas, funciones o peticiones vistas desde el navegador, con sus percentiles (paginado)",
)
def performance_metrics(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    kind: Annotated[MetricKind, Query(description="HTTP (servidor), FUNCTION o WEB_API (navegador)")],
    period: Period = "24h",
    sort: Annotated[MetricSort, Query(description="Orden: la mayor primero")] = "impact",
    search: Search = None,
) -> ApiResponse[MetricList]:
    result = PerformanceService(db).metrics(kind, period, sort=sort, search=search, page=page)
    return ok(result, code="PERFORMANCE_METRICS", params={"count": result.total})


@router.get("/metrics/series", response_model=ApiResponse[MetricSeries], summary="Una ruta o función en el tiempo")
def performance_series(
    _: AdminUser,
    db: DbSession,
    kind: Annotated[MetricKind, Query(description="HTTP, FUNCTION o WEB_API")],
    name: Annotated[str, Query(min_length=1, max_length=200, description="`GET /api/...` o `face.detect`")],
    period: Period = "24h",
) -> ApiResponse[MetricSeries]:
    return ok(PerformanceService(db).series(kind, name, period), code="PERFORMANCE_SERIES")


@router.get(
    "/web-vitals",
    response_model=ApiResponse[WebVitalsList],
    summary="Web Vitals y tareas largas de cada pantalla de la aplicación web (paginado)",
)
def performance_web_vitals(
    _: AdminUser, db: DbSession, page: Pagination, period: Period = "24h"
) -> ApiResponse[WebVitalsList]:
    result = PerformanceService(db).web_vitals(period, page)
    return ok(result, code="PERFORMANCE_WEB_VITALS", params={"count": result.total})


@router.get(
    "/statements",
    response_model=ApiResponse[StatementList],
    summary="Consultas que más consumen la base (pg_stat_statements, texto normalizado; paginado)",
)
def performance_statements(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    sort: Annotated[StatementSort, Query(description="total, mean, max o calls")] = "total",
) -> ApiResponse[StatementList]:
    result = PerformanceService(db).statements(sort, page)
    return ok(result, code="PERFORMANCE_STATEMENTS", params={"count": result.total})


@router.get(
    "/alerts",
    response_model=ApiResponse[SlowAlertList],
    summary="Alertas de peticiones lentas por ruta (paginado, lo más reciente primero)",
)
def slow_alerts(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    status: Annotated[SlowAlertStatus | None, Query(description="Filtrar por seguimiento")] = None,
    search: Search = None,
) -> ApiResponse[SlowAlertList]:
    result = PerformanceService(db).alerts(status=status, search=search, page=page)
    return ok(result, code="SLOW_ALERTS_LISTED", params={"count": result.total})


@router.get(
    "/alerts/summary",
    response_model=ApiResponse[SlowAlertSummary],
    summary="Abiertas, en atención y la más reciente (contador del menú y aviso en vivo)",
)
def slow_alerts_summary(_: AdminUser, db: DbSession) -> ApiResponse[SlowAlertSummary]:
    return ok(PerformanceService(db).summary(), code="SLOW_ALERTS_SUMMARY")


@router.get(
    "/alerts/{alert_id}",
    response_model=ApiResponse[SlowAlertDetail],
    summary="Detalle de una alerta (muestra, últimas 24 h y el error de su traceId)",
    responses=NOT_FOUND,
)
def slow_alert(alert_id: int, _: AdminUser, db: DbSession) -> ApiResponse[SlowAlertDetail]:
    return ok(PerformanceService(db).alert(alert_id), code="SLOW_ALERT_FOUND")


@router.patch(
    "/alerts/{alert_id}",
    response_model=ApiResponse[SlowAlertDetail],
    summary="Cambiar el seguimiento: abierta, en atención o resuelta",
    responses=NOT_FOUND,
)
def set_slow_alert_status(
    alert_id: int, payload: SlowAlertStatusUpdate, admin: AdminUser, db: DbSession
) -> ApiResponse[SlowAlertDetail]:
    result = PerformanceService(db).set_status(alert_id, payload.status, admin)
    return ok(result, code="SLOW_ALERT_STATUS_UPDATED")
