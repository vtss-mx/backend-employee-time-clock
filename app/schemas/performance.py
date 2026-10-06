"""Observabilidad de rendimiento (pantalla "Rendimiento" del ADMIN) y lo que manda el navegador.

Tiempos en milisegundos (un decimal); bytes como enteros (la app los muestra en MB). Los periodos y órdenes son
literales: un valor desconocido es un 422 por campo, igual que el resto de la API.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import SlowAlertStatus
from app.schemas.common import Page

PerfPeriod = Literal["1h", "6h", "24h", "7d", "30d", "90d"]
MetricKind = Literal["HTTP", "FUNCTION", "WEB_API"]
MetricSort = Literal["impact", "p95", "mean", "max", "count", "errors"]
StatementSort = Literal["total", "mean", "max", "calls"]
WebSampleKind = Literal["LCP", "INP", "CLS", "FCP", "TTFB", "LONG_TASK", "API"]

#: Tope del cuerpo JSON de un lote del navegador (holgado para 500 muestras de ~100 bytes).
MAX_WEB_BODY_BYTES = 64 * 1024


class MetricRow(BaseModel):
    """Una ruta (HTTP o vista desde el navegador) o una función en el periodo."""

    #: HTTP, FUNCTION o WEB_API.
    kind: str
    name: str
    count: int
    #: HTTP: 5xx · FUNCTION: excepciones · WEB_API: 5xx, sin red (0) y tiempo agotado (408).
    errors: int
    #: 4xx (HTTP y WEB_API).
    client_errors: int
    error_rate: float
    avg_ms: float
    max_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    total_ms: float
    #: % del tiempo total de su tipo en el periodo.
    share: float
    #: Solo HTTP: tiempo y sentencias de la BD por petición y % de su tiempo dentro de la BD.
    avg_db_ms: float | None = None
    avg_queries: float | None = None
    db_share: float | None = None
    bytes_in: int = 0
    bytes_out: int = 0


class PerfTotals(BaseModel):
    requests: int
    client_errors: int
    server_errors: int
    #: % de las peticiones que fueron 5xx y 4xx.
    error_rate: float
    client_error_rate: float
    throughput_per_minute: float
    avg_ms: float
    max_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    #: % del tiempo de las peticiones dentro de la BD, tiempo y sentencias promedio.
    db_share: float
    avg_db_ms: float
    avg_queries: float
    bytes_in: int
    bytes_out: int


class PerfPoint(BaseModel):
    """Un punto de la serie (inicio del intervalo)."""

    at: datetime
    requests: int
    client_errors: int
    server_errors: int
    avg_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    db_share: float


class PerformanceOverview(BaseModel):
    period: str
    start: datetime
    end: datetime
    step_seconds: int
    #: El umbral de la regla 18 (`SLOW_REQUEST_THRESHOLD_MS`).
    slow_threshold_ms: int
    #: El de las rutas faciales (`SLOW_REQUEST_FACE_THRESHOLD_MS`, excepción que decidió el dueño del producto).
    slow_face_threshold_ms: int
    open_alerts: int
    totals: PerfTotals
    points: list[PerfPoint]
    top_routes: list[MetricRow]
    top_functions: list[MetricRow]


class MetricList(Page[MetricRow]):
    """Rutas o funciones del periodo en el orden pedido."""

    kind: str
    period: str
    sort: str


class MetricPoint(BaseModel):
    at: datetime
    count: int
    errors: int
    client_errors: int
    avg_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


class MetricSeries(BaseModel):
    kind: str
    name: str
    period: str
    start: datetime
    end: datetime
    step_seconds: int
    totals: MetricRow
    points: list[MetricPoint]
    #: HTTP: la alerta de peticiones lentas de esa ruta, si existe.
    slow_alert_id: int | None = None


class Vital(BaseModel):
    """Un Web Vital de una pantalla: su p75 (el que usa Google) y su calificación con los umbrales de Google."""

    count: int
    p75: float
    #: "ms" o "score" (CLS).
    unit: str
    #: GOOD, NEEDS_IMPROVEMENT o POOR.
    rating: str
    good: float
    poor: float


class LongTasks(BaseModel):
    count: int
    total_ms: float
    p95_ms: float
    max_ms: float


class ScreenVitals(BaseModel):
    #: Plantilla de la pantalla (`/admin/companies/{id}`).
    screen: str
    #: Muestras de la pantalla (las del Web Vital con más muestras).
    views: int
    lcp: Vital | None = None
    inp: Vital | None = None
    cls: Vital | None = None
    fcp: Vital | None = None
    ttfb: Vital | None = None
    long_tasks: LongTasks


class WebVitalsList(Page[ScreenVitals]):
    """Pantallas de la aplicación web con sus Web Vitals (la de más muestras primero)."""

    period: str


class Statement(BaseModel):
    """Una consulta de `pg_stat_statements` (texto normalizado: nunca parámetros)."""

    query_id: str
    query: str
    calls: int
    total_ms: float
    mean_ms: float
    max_ms: float
    rows: int
    #: % del tiempo total de la base.
    share: float


class StatementList(Page[Statement]):
    available: bool
    sort: str


class SlowAlert(BaseModel):
    """Las peticiones lentas de una ruta (regla 18), agrupadas."""

    id: int
    route: str
    method: str
    path: str
    status: SlowAlertStatus
    count: int
    avg_ms: float
    last_ms: float
    max_ms: float
    threshold_ms: int
    first_seen_at: datetime
    last_seen_at: datetime
    opened_at: datetime
    last_trace_id: str | None = None
    last_status: int | None = None
    reopened: int


class SlowAlertDetail(SlowAlert):
    #: Contexto de la última (sin secretos ni cuerpos): método, ruta, query, estado, tiempos, BD, bytes, rol, empresa.
    sample: dict[str, Any] | None = None
    status_changed_at: datetime | None = None
    status_changed_by: str | None = None
    requests_24h: int
    p95_ms_24h: float
    #: El error registrado con el traceId de la última (si lo hay): `/admin/errors/{id}`.
    error_report_id: int | None = None


class SlowAlertList(Page[SlowAlert]):
    #: Cuándo se armó la lista (lo más reciente primero).
    as_of: datetime


class LatestAlert(BaseModel):
    id: int
    route: str
    opened_at: datetime
    last_ms: float


class SlowAlertSummary(BaseModel):
    """El contador del menú y el aviso en vivo: abiertas, en atención y la abierta más reciente."""

    open: int
    acknowledged: int
    latest: LatestAlert | None = None


class SlowAlertStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SlowAlertStatus


class WebSample(BaseModel):
    """Una medición del navegador (sin datos de la persona: plantillas, tiempos y códigos)."""

    model_config = ConfigDict(extra="forbid")

    kind: WebSampleKind
    name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Web Vitals y LONG_TASK: plantilla de la pantalla (`/admin/companies/{id}`). API: `MÉTODO /api/ruta` "
            "con los ids como `{id}`"
        ),
    )
    value: float = Field(ge=0, le=600_000, description="Milisegundos (CLS: el puntaje sin unidad)")
    status: int | None = Field(default=None, ge=0, le=599, description="Solo API: código HTTP; 0 sin red, 408 agotado")


class WebPerfBatch(BaseModel):
    """Lo que el navegador midió desde su lote anterior (Web Vitals, tareas largas y sus peticiones)."""

    model_config = ConfigDict(extra="forbid")

    app_version: str | None = Field(default=None, max_length=64)
    samples: list[WebSample] = Field(min_length=1, max_length=5000)


class WebPerfResult(BaseModel):
    accepted: int
    #: Las que no se tomaron en cuenta (sin sesión fuera del inicio de sesión, nombre que no es una plantilla).
    dropped: int
