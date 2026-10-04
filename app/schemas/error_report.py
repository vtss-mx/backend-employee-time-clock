from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import ErrorStatus
from app.schemas.common import Page


class ErrorReportRead(BaseModel):
    """Un error del sistema agrupado (todas sus ocurrencias iguales)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str = Field(description="HTTP, LOG (segundo plano o dentro de un proceso) o WEBSOCKET")
    severity: str = Field(description="catalog.error_severities")
    status: str = Field(description="catalog.error_statuses")
    code: str
    message: str
    http_status: int | None = None
    method: str | None = None
    location: str | None = Field(default=None, description="Ruta sin ids o archivo:línea")
    exception_type: str | None = None
    occurrences: int
    reopened: int = Field(description="Veces que volvió a ocurrir después de marcarse como solucionado")
    first_seen_at: datetime
    last_seen_at: datetime
    last_trace_id: str | None = None
    status_changed_at: datetime | None = None
    status_changed_by: str | None = None


class ErrorReportDetail(ErrorReportRead):
    detail: str | None = Field(default=None, description="Detalle técnico (stack trace) de la última ocurrencia")


class ErrorReportList(Page[ErrorReportRead]):
    """Errores del sistema (lo más reciente primero)."""


class ErrorOccurrenceRead(BaseModel):
    id: int
    occurred_at: datetime
    trace_id: str | None = None
    message: str
    #: Quién lo provocó, tal cual: «correo (Rol)».
    user_label: str | None = None
    company_name: str | None = None
    #: Contexto literal: la petición (método, URL, encabezados, cuerpo), la respuesta (estado y
    #: cuerpo), el usuario y la empresa. Sin secretos ni archivos.
    context: dict[str, Any] | None = None


class ErrorOccurrenceList(Page[ErrorOccurrenceRead]):
    """Ocurrencias recientes de un error (la más reciente primero)."""


class ErrorSummary(BaseModel):
    """Conteos para el contador del menú y los filtros de la bandeja."""

    by_status: dict[str, int]
    open_by_severity: dict[str, int]
    pending: int
    last_seen_at: datetime | None = None


class ErrorStatusUpdate(BaseModel):
    status: ErrorStatus


class DemandRead(BaseModel):
    """Una API (método + ruta sin ids) y su demanda reciente en este proceso."""

    api: str
    tier: str
    recent_requests: float
    latency_ms: float | None = None
    shed: int


class AdmissionRead(BaseModel):
    """Control de admisión adaptativo de este proceso (app/core/admission.py)."""

    limit: int
    bounds: list[int]
    in_flight: int
    waiting: int
    admitted: int
    shed: int
    latency_ratio: float | None = None
    top_demand: list[DemandRead]


class ServerStatus(BaseModel):
    """Estado del servidor para el ADMIN: dependencias con su detalle y capacidad del proceso."""

    status: str
    components: dict[str, dict[str, Any]]
    admission: AdmissionRead
