"""Asistente de reportes de la empresa (solo COMPANY, pantalla "Reportes").

Preguntas en español sobre los datos de SU empresa, vista previa, exportación a Excel, reportes
guardados y lo que el asistente aprende. La empresa sale siempre de la sesión (`CompanyScope`):
ningún parámetro permite consultar otra.
"""

from typing import Any

from fastapi import APIRouter, Depends, Response, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, CompanyUser, DbSession, Pagination, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.report import (
    AskRequest,
    ExportRequest,
    FeedbackRequest,
    FeedbackResult,
    PreviewRequest,
    ReportAnswer,
    ReportCatalog,
    SavedReportCreate,
    SavedReportList,
    SavedReportRead,
)
from app.services.reporting.excel import MEDIA_TYPE
from app.services.reporting.service import ReportAssistant

router = APIRouter(
    prefix="/reports",
    tags=["Reportes (empresa)"],
    dependencies=[Depends(require_screen(Screen.COMPANY_REPORTS))],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Solo la empresa"},
        422: {"model": ErrorResponse, "description": "Plan de reporte inválido"},
    },
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "No existe en tu empresa"}}


def _assistant(db: DbSession, user: CompanyUser, company_id: CompanyScope) -> ReportAssistant:
    return ReportAssistant(db, company_id, user)


Assistant = Depends(_assistant)


@router.get(
    "/catalog",
    response_model=ApiResponse[ReportCatalog],
    summary="Qué datos se pueden consultar y sugerencias",
    description="Reportes disponibles con sus columnas, valores para filtrar y ejemplos; sugerencias "
    "aprendidas de lo que más pregunta la empresa.",
)
def catalog(assistant: ReportAssistant = Assistant) -> ApiResponse[ReportCatalog]:
    return ok(assistant.catalog(), "Catálogo de reportes", code="REPORT_CATALOG")


@router.post(
    "/ask",
    response_model=ApiResponse[ReportAnswer],
    summary="Preguntar al asistente",
    description="Una pregunta en español («¿cuántas identificaciones fallidas hubo ayer por motivo?»). "
    "Siempre responde: con datos y su vista previa si entendió; si no, con lo que hay y cómo preguntar. "
    "`context` (el plan anterior) permite seguir la conversación.",
)
def ask(body: AskRequest, assistant: ReportAssistant = Assistant) -> ApiResponse[ReportAnswer]:
    return ok(assistant.ask(body.question, body.context), "Respuesta del asistente", code="REPORT_ANSWER")


@router.post("/preview", response_model=ApiResponse[ReportAnswer], summary="Vista previa de un plan de reporte")
def preview(body: PreviewRequest, assistant: ReportAssistant = Assistant) -> ApiResponse[ReportAnswer]:
    return ok(assistant.preview(body.plan), "Vista previa del reporte", code="REPORT_PREVIEW")


@router.post(
    "/export",
    response_class=Response,
    summary="Exportar a Excel",
    description="Devuelve el archivo .xlsx (hoja «Datos» y hoja «Resumen»). Es la única respuesta que no es "
    "el sobre JSON: es un archivo. Cualquier error sí responde con el sobre de siempre.",
    responses={200: {"content": {MEDIA_TYPE: {}}, "description": "Archivo de Excel"}},
)
def export(body: ExportRequest, assistant: ReportAssistant = Assistant) -> Response:
    file = assistant.export(body.plan, query_id=body.query_id, question=body.question)
    return Response(
        content=file.content,
        media_type=MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{file.filename}"'},
    )


@router.post(
    "/feedback",
    response_model=ApiResponse[FeedbackResult],
    summary="¿Sirvió? / ¿a qué datos te referías?",
    description="El asistente aprende de la empresa: si no entendió y la persona elige los datos, la próxima "
    "vez entiende esas palabras (y responde de nuevo); si la respuesta no sirvió, deja de usarlas.",
    responses=NOT_FOUND,
)
def feedback(body: FeedbackRequest, assistant: ReportAssistant = Assistant) -> ApiResponse[FeedbackResult]:
    learned, answer = assistant.feedback(body.query_id, helpful=body.helpful, dataset=body.dataset)
    return ok(FeedbackResult(learned=learned, answer=answer), "Gracias, lo tomo en cuenta", code="REPORT_FEEDBACK")


@router.get("/saved", response_model=ApiResponse[SavedReportList], summary="Reportes guardados")
def saved(page: Pagination, assistant: ReportAssistant = Assistant) -> ApiResponse[SavedReportList]:
    result = assistant.saved(page)
    return ok(result, f"{result.total} reporte(s) guardado(s)", code="SAVED_REPORTS")


@router.post(
    "/saved",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[SavedReportRead],
    summary="Guardar un reporte",
    responses={409: {"model": ErrorResponse, "description": "Ya hay uno con ese nombre"}},
)
def save(body: SavedReportCreate, assistant: ReportAssistant = Assistant) -> ApiResponse[SavedReportRead]:
    return ok(assistant.save(body), "Reporte guardado", code="SAVED_REPORT_CREATED", status_code=201)


@router.post(
    "/saved/{report_id}/run",
    response_model=ApiResponse[ReportAnswer],
    summary="Generar de nuevo un reporte guardado (datos al día)",
    responses=NOT_FOUND,
)
def run_saved(report_id: int, assistant: ReportAssistant = Assistant) -> ApiResponse[ReportAnswer]:
    return ok(assistant.run_saved(report_id), "Reporte generado", code="REPORT_PREVIEW")


@router.delete(
    "/saved/{report_id}", response_model=ApiResponse[None], summary="Borrar un reporte guardado", responses=NOT_FOUND
)
def delete_saved(report_id: int, assistant: ReportAssistant = Assistant) -> ApiResponse[None]:
    assistant.delete_saved(report_id)
    return ok(None, "Reporte guardado eliminado", code="SAVED_REPORT_DELETED")
