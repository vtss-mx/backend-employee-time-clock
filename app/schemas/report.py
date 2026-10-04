"""Contrato del asistente de reportes (pantalla "Reportes" de COMPANY).

El `ReportPlan` es lo que se consulta exactamente: lo arma el asistente a partir de una pregunta o
la persona con el constructor guiado, y el backend SIEMPRE lo valida contra el catálogo (columnas,
filtros y valores permitidos) antes de consultar. Nunca lleva la empresa: sale de la sesión.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Page

FilterOp = Literal["in", "not_in", "contains", "gt", "gte", "lt", "lte"]
Scalar = bool | int | float | str


class ReportFilter(BaseModel):
    column: str = Field(max_length=40)
    op: FilterOp = "in"
    values: list[Scalar] = Field(default_factory=list, max_length=50)


class ReportPeriod(BaseModel):
    """[start, end): el fin no se incluye. Sin inicio = desde el principio. Un momento sin zona
    horaria es de la hora del negocio (p. ej. «2026-10-01T00:00:00» = medianoche en la empresa)."""

    start: datetime | None = None
    end: datetime | None = None
    label: str = Field(default="", max_length=120)


class ReportPlan(BaseModel):
    dataset: str = Field(max_length=40)
    #: Columnas a mostrar (vacío: las de por omisión del catálogo).
    columns: list[str] = Field(default_factory=list, max_length=30)
    filters: list[ReportFilter] = Field(default_factory=list, max_length=20)
    period: ReportPeriod | None = None
    #: rows: el detalle; count: cuántos; groups: totales por una o dos columnas.
    mode: Literal["rows", "count", "groups"] = "rows"
    group_by: list[str] = Field(default_factory=list, max_length=2)
    metric: Literal["count", "sum", "avg", "min", "max"] = "count"
    metric_column: str | None = Field(default=None, max_length=40)
    #: Orden: una columna, o "metric" para ordenar los grupos por su total.
    sort: str | None = Field(default=None, max_length=40)
    descending: bool | None = None
    #: Tope pedido («los 5 que más...»).
    limit: int | None = Field(default=None, ge=1, le=1000)


class ReportColumnRead(BaseModel):
    code: str
    label: str
    kind: str


class ReportPreview(BaseModel):
    """Lo que se ve en pantalla: las primeras filas (o todos los grupos) y el total."""

    columns: list[ReportColumnRead]
    rows: list[list[Any]]
    total: int
    truncated: bool


class DatasetOption(BaseModel):
    code: str
    name: str


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    #: El plan de la respuesta anterior: permite seguir la conversación («y por departamento»).
    context: ReportPlan | None = None


class ReportAnswer(BaseModel):
    """Respuesta del asistente: siempre trae texto; con datos, su plan y la vista previa."""

    query_id: int | None = None
    answer: str
    #: Hallazgos calculados de los datos («Producción concentra el 43 %»).
    highlights: list[str] = Field(default_factory=list)
    #: Cómo se entendió la pregunta («Datos: Identificaciones», «Periodo: ayer»).
    understood: list[str] = Field(default_factory=list)
    plan: ReportPlan | None = None
    preview: ReportPreview | None = None
    #: Otros datos a los que pudo referirse (para aclarar con un clic).
    alternatives: list[DatasetOption] = Field(default_factory=list)
    #: La pregunta pidió el archivo de Excel.
    export: bool = False
    suggestions: list[str] = Field(default_factory=list)


class PreviewRequest(BaseModel):
    plan: ReportPlan


class ExportRequest(BaseModel):
    plan: ReportPlan
    query_id: int | None = None
    #: La pregunta original, para la hoja de resumen del archivo.
    question: str | None = Field(default=None, max_length=500)


class FeedbackRequest(BaseModel):
    """¿Sirvió la respuesta? y, si no se entendió, a qué datos se refería: el asistente aprende."""

    query_id: int
    helpful: bool | None = None
    dataset: str | None = Field(default=None, max_length=40)


class ColumnOption(BaseModel):
    value: Scalar
    label: str


class CatalogColumn(BaseModel):
    code: str
    label: str
    kind: str
    group: bool
    default: bool
    metric: bool
    options: list[ColumnOption] = Field(default_factory=list)


class CatalogDataset(BaseModel):
    code: str
    name: str
    description: str
    time: str | None
    columns: list[CatalogColumn]
    examples: list[str]


class ReportCatalog(BaseModel):
    datasets: list[CatalogDataset]
    #: Lo que más pregunta la empresa (aprendido) y ejemplos para empezar.
    suggestions: list[str]


class SavedReportCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    plan: ReportPlan
    question: str | None = Field(default=None, max_length=500)
    #: La respuesta del asistente de la que nace (guardarla confirma que se entendió).
    query_id: int | None = None


class FeedbackResult(BaseModel):
    #: Lo que aprendió («checadas» → Identificaciones).
    learned: list[str]
    #: La respuesta de nuevo, ya con lo aprendido (si se aclaró a qué datos se refería).
    answer: ReportAnswer | None = None


class SavedReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    question: str | None = None
    dataset: str
    dataset_name: str
    plan: ReportPlan
    created_at: datetime
    last_run_at: datetime | None = None
    runs: int


class SavedReportList(Page[SavedReportRead]):
    """Reportes guardados de la empresa (por nombre)."""
