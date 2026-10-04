"""Errores del sistema (solo el ADMIN de la plataforma): bandeja, detalle y seguimiento."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.responses import ApiResponse, ok
from app.dependencies import AdminUser, DbSession, Pagination, require_screen
from app.models import ErrorSeverity, ErrorStatus, Screen
from app.schemas.common import ErrorResponse
from app.schemas.error_report import (
    ErrorOccurrenceList,
    ErrorReportDetail,
    ErrorReportList,
    ErrorStatusUpdate,
    ErrorSummary,
    ServerStatus,
)
from app.services import health_service
from app.services.error_report_service import ErrorReportService

router = APIRouter(
    prefix="/admin/errors",
    tags=["Errores del sistema (ADMIN)"],
    dependencies=[Depends(require_screen(Screen.ADMIN_ERRORS))],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Solo el ADMIN de la plataforma"},
    },
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Error no encontrado"}}


@router.get(
    "", response_model=ApiResponse[ErrorReportList], summary="Errores del sistema (paginado, lo más reciente primero)"
)
def list_errors(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    status: Annotated[ErrorStatus | None, Query(description="Filtrar por seguimiento")] = None,
    severity: Annotated[ErrorSeverity | None, Query(description="Filtrar por gravedad")] = None,
    search: Annotated[str | None, Query(max_length=100, description="Código, mensaje, ruta o excepción")] = None,
) -> ApiResponse[ErrorReportList]:
    result = ErrorReportService(db).list_reports(status=status, severity=severity, search=search, page=page)
    return ok(result, f"{result.total} error(es)", code="ERROR_REPORTS_LISTED")


@router.get(
    "/summary", response_model=ApiResponse[ErrorSummary], summary="Conteos por estado y gravedad (menú y filtros)"
)
def errors_summary(_: AdminUser, db: DbSession) -> ApiResponse[ErrorSummary]:
    return ok(ErrorReportService(db).summary(), "Resumen de errores", code="ERROR_SUMMARY")


@router.get(
    "/server",
    response_model=ApiResponse[ServerStatus],
    summary="Estado del servidor: dependencias y capacidad adaptativa",
    description=(
        "Detalle que las sondas públicas no muestran: el error de cada componente (BD, motor facial y "
        "su fila) y el control de admisión de este proceso (límite vigente, fila, descartes y las APIs "
        "con más demanda)."
    ),
)
def server_status(_: AdminUser) -> ApiResponse[ServerStatus]:
    return ok(ServerStatus.model_validate(health_service.server_status()), "Estado del servidor", code="SERVER_STATUS")


@router.get(
    "/{report_id}",
    response_model=ApiResponse[ErrorReportDetail],
    summary="Detalle (con stack trace)",
    responses=NOT_FOUND,
)
def get_error(report_id: int, _: AdminUser, db: DbSession) -> ApiResponse[ErrorReportDetail]:
    return ok(ErrorReportService(db).detail(report_id), "Error encontrado", code="ERROR_REPORT_FOUND")


@router.get(
    "/{report_id}/occurrences",
    response_model=ApiResponse[ErrorOccurrenceList],
    summary="Ocurrencias recientes (quién, cuándo, empresa y traceId)",
    responses=NOT_FOUND,
)
def error_occurrences(
    report_id: int, _: AdminUser, db: DbSession, page: Pagination
) -> ApiResponse[ErrorOccurrenceList]:
    result = ErrorReportService(db).occurrences(report_id, page)
    return ok(result, f"{result.total} ocurrencia(s)", code="ERROR_OCCURRENCES_LISTED")


@router.patch(
    "/{report_id}/status",
    response_model=ApiResponse[ErrorReportDetail],
    summary="Marcar el seguimiento: pendiente, en proceso, en revisión o solucionado",
    responses=NOT_FOUND,
)
def set_error_status(
    report_id: int, payload: ErrorStatusUpdate, admin: AdminUser, db: DbSession
) -> ApiResponse[ErrorReportDetail]:
    result = ErrorReportService(db).set_status(report_id, payload.status, admin)
    return ok(result, "Seguimiento actualizado", code="ERROR_STATUS_UPDATED")
