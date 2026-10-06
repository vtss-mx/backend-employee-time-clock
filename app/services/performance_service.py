"""Pantalla "Rendimiento" del ADMIN: cómo se comportan las APIs, las funciones clave, la base de datos y las
pantallas de la aplicación web, y las alertas de peticiones lentas (regla 18).

Los periodos eligen su grano para leer pocas filas: la última hora y las últimas 6 horas, por minuto (lo más
fresco: lo que guardó el último lote); 24 horas y 7 días, por hora; 30 y 90 días, por día (las horas y los días los
resume el mantenimiento: se ven con el retraso de su vuelta, `MAINTENANCE_INTERVAL_SECONDS`). Los percentiles salen
de sumar los histogramas del periodo (`app/core/histogram.py`); nada se calcula fila por fila en Python más allá de
lo acotado (≤ 1 440 minutos, ≤ 168 horas, ≤ 90 días o una página de nombres).
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy.orm import Session

from app.core.clock import as_utc, business_date, business_day_bounds
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.perf_meter import Stat
from app.models import PerfKind, SlowAlertStatus, SlowRequestAlert, User
from app.repositories.performance_repository import DAYS, HOURS, MINUTES, Grain, PerformanceRepository
from app.schemas.common import PageParams
from app.schemas.performance import (
    LatestAlert,
    LongTasks,
    MetricList,
    MetricPoint,
    MetricRow,
    MetricSeries,
    PerformanceOverview,
    PerfPoint,
    PerfTotals,
    ScreenVitals,
    SlowAlert,
    SlowAlertDetail,
    SlowAlertList,
    SlowAlertSummary,
    Statement,
    StatementList,
    Vital,
    WebVitalsList,
)
from app.services.web_performance import CLS_SCALE


@dataclass(frozen=True)
class Window:
    """Un periodo: su grano, el rango [start, end) en UTC, el ancho de cada punto y los límites de la consulta (en
    el grano por día, fechas del negocio)."""

    period: str
    grain: Grain
    start: datetime
    end: datetime
    step: int
    since: Any
    until: Any

    @property
    def points(self) -> int:
        return max(1, int((self.end - self.start).total_seconds()) // self.step)

    def index(self, at: Any) -> int:
        """Punto de la serie al que pertenece una fila del grano."""
        if isinstance(at, datetime):
            return int((as_utc(at) - self.start).total_seconds()) // self.step
        return (at - self.since).days

    def moment(self, index: int) -> datetime:
        return self.start + timedelta(seconds=index * self.step)


#: Periodo → (duración, grano, ancho de cada punto en segundos).
PERIODS: Final = {
    "1h": (timedelta(hours=1), MINUTES, 60),
    "6h": (timedelta(hours=6), MINUTES, 300),
    "24h": (timedelta(hours=24), HOURS, 3600),
    "7d": (timedelta(days=7), HOURS, 3 * 3600),
    "30d": (timedelta(days=30), DAYS, 86400),
    "90d": (timedelta(days=90), DAYS, 86400),
}
#: Rutas y funciones que muestra el resumen (la lista completa va paginada).
TOP: Final = 5
#: Ventana del detalle de una alerta (sus peticiones y su p95) y la búsqueda de su error por traceId.
ALERT_WINDOW: Final = timedelta(hours=24)
TRACE_WINDOW: Final = timedelta(minutes=10)


@dataclass(frozen=True)
class VitalSpec:
    """Un Web Vital: su tipo, su unidad y los umbrales de Google (bueno hasta `good`, malo desde `poor`)."""

    kind: PerfKind
    unit: str
    good: float
    poor: float


#: Umbrales de Google para el p75 de cada Web Vital (web.dev/vitals): una definición del estándar, no configuración.
VITALS: Final = {
    "lcp": VitalSpec(PerfKind.WEB_LCP, "ms", 2500, 4000),
    "inp": VitalSpec(PerfKind.WEB_INP, "ms", 200, 500),
    "cls": VitalSpec(PerfKind.WEB_CLS, "score", 0.1, 0.25),
    "fcp": VitalSpec(PerfKind.WEB_FCP, "ms", 1800, 3000),
    "ttfb": VitalSpec(PerfKind.WEB_TTFB, "ms", 800, 1800),
}


def window(period: str, now: datetime | None = None) -> Window:
    """El rango del periodo que termina ahora (el minuto, la hora o el día en curso incluidos)."""
    moment = now or datetime.now(UTC)
    span, grain, step = PERIODS[period]
    if grain is DAYS:
        last = business_date(moment)
        first = last - timedelta(days=span.days - 1)
        start, end = business_day_bounds(first)[0], business_day_bounds(last)[1]
        return Window(period, grain, start, end, step, first, last + timedelta(days=1))
    unit = 60 if grain is MINUTES else 3600
    end = datetime.fromtimestamp((int(moment.timestamp()) // unit + 1) * unit, UTC)
    start = end - span
    return Window(period, grain, start, end, step, start, end)


def _ratio(part: float, whole: float, scale: float = 100.0) -> float:
    return round(part * scale / whole, 2) if whole else 0.0


def metric_row(kind: str, name: str, totals: Stat, kind_total_ms: float) -> MetricRow:
    """Una ruta o función con sus percentiles y su parte del tiempo de su tipo."""
    http = kind == PerfKind.HTTP
    return MetricRow(
        kind=kind,
        name=name,
        count=totals.count,
        errors=totals.errors,
        client_errors=totals.client_errors,
        error_rate=_ratio(totals.errors, totals.count),
        avg_ms=totals.avg_ms,
        max_ms=round(totals.max_ms, 1),
        p50_ms=totals.quantile(0.5),
        p95_ms=totals.quantile(0.95),
        p99_ms=totals.quantile(0.99),
        total_ms=round(totals.total_ms, 1),
        share=_ratio(totals.total_ms, kind_total_ms),
        avg_db_ms=round(totals.db_ms / totals.count, 1) if http and totals.count else (0.0 if http else None),
        avg_queries=round(totals.db_queries / totals.count, 2) if http and totals.count else (0.0 if http else None),
        db_share=_ratio(totals.db_ms, totals.total_ms) if http else None,
        bytes_in=totals.bytes_in,
        bytes_out=totals.bytes_out,
    )


def _rating(value: float, spec: VitalSpec) -> str:
    if value <= spec.good:
        return "GOOD"
    return "NEEDS_IMPROVEMENT" if value <= spec.poor else "POOR"


def vital(spec: VitalSpec, totals: Stat | None) -> Vital | None:
    """El p75 de un Web Vital y su calificación (el CLS vuelve a su escala de puntaje)."""
    if totals is None or not totals.count:
        return None
    scale = CLS_SCALE if spec.kind == PerfKind.WEB_CLS else 1
    p75 = round(totals.quantile(0.75) / scale, 3)
    return Vital(
        count=totals.count,
        p75=p75,
        unit=spec.unit,
        rating=_rating(p75, spec),
        good=spec.good,
        poor=spec.poor,
    )


class PerformanceService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = PerformanceRepository(db)

    # ---------- Resumen ----------

    def overview(self, period: str) -> PerformanceOverview:
        """Las peticiones de toda la plataforma en el periodo: totales, serie, las rutas más lentas y las funciones
        que más tiempo consumen, y cuántas alertas siguen abiertas."""
        span = window(period)
        rows = self.repo.series(span.grain, (PerfKind.HTTP.value,), span.since, span.until)
        points = [Stat() for _ in range(span.points)]
        whole = Stat()
        for row in rows:
            totals = Stat.of(row)
            whole.add(totals)
            points[span.index(row.at)].add(totals)
        minutes = (span.end - span.start).total_seconds() / 60
        return PerformanceOverview(
            period=period,
            start=span.start,
            end=span.end,
            step_seconds=span.step,
            slow_threshold_ms=settings.SLOW_REQUEST_THRESHOLD_MS,
            slow_face_threshold_ms=settings.SLOW_REQUEST_FACE_THRESHOLD_MS,
            open_alerts=self.repo.alert_counts().get(SlowAlertStatus.OPEN.value, 0),
            totals=self._totals(whole, minutes),
            points=[self._point(span.moment(i), point) for i, point in enumerate(points)],
            top_routes=self._top(span, PerfKind.HTTP.value, "p95"),
            top_functions=self._top(span, PerfKind.FUNCTION.value, "impact"),
        )

    @staticmethod
    def _totals(whole: Stat, minutes: float) -> PerfTotals:
        return PerfTotals(
            requests=whole.count,
            client_errors=whole.client_errors,
            server_errors=whole.errors,
            error_rate=_ratio(whole.errors, whole.count),
            client_error_rate=_ratio(whole.client_errors, whole.count),
            throughput_per_minute=round(whole.count / minutes, 2),
            avg_ms=whole.avg_ms,
            max_ms=round(whole.max_ms, 1),
            p50_ms=whole.quantile(0.5),
            p95_ms=whole.quantile(0.95),
            p99_ms=whole.quantile(0.99),
            db_share=_ratio(whole.db_ms, whole.total_ms),
            avg_db_ms=round(whole.db_ms / whole.count, 1) if whole.count else 0.0,
            avg_queries=round(whole.db_queries / whole.count, 2) if whole.count else 0.0,
            bytes_in=whole.bytes_in,
            bytes_out=whole.bytes_out,
        )

    @staticmethod
    def _point(at: datetime, totals: Stat) -> PerfPoint:
        return PerfPoint(
            at=at,
            requests=totals.count,
            client_errors=totals.client_errors,
            server_errors=totals.errors,
            avg_ms=totals.avg_ms,
            p50_ms=totals.quantile(0.5),
            p95_ms=totals.quantile(0.95),
            p99_ms=totals.quantile(0.99),
            max_ms=round(totals.max_ms, 1),
            db_share=_ratio(totals.db_ms, totals.total_ms),
        )

    def _top(self, span: Window, kind: str, sort: str) -> list[MetricRow]:
        rows, _, kind_total = self.repo.metrics_page(
            span.grain, kind, span.since, span.until, sort=sort, search=None, offset=0, limit=TOP
        )
        return [metric_row(kind, row.name, Stat.of(row), kind_total) for row in rows]

    # ---------- Rutas, funciones y lo que ve el navegador ----------

    def metrics(self, kind: str, period: str, *, sort: str, search: str | None, page: PageParams) -> MetricList:
        span = window(period)
        rows, total, kind_total = self.repo.metrics_page(
            span.grain, kind, span.since, span.until, sort=sort, search=search, offset=page.offset, limit=page.size
        )
        items = [metric_row(kind, row.name, Stat.of(row), kind_total) for row in rows]
        return MetricList.of(items, total, page, kind=kind, period=period, sort=sort)

    def series(self, kind: str, name: str, period: str) -> MetricSeries:
        """Una ruta o función: sus totales del periodo y su serie (con ceros donde no hubo nada)."""
        span = window(period)
        points = [Stat() for _ in range(span.points)]
        whole = Stat()
        for row in self.repo.series(span.grain, (kind,), span.since, span.until, name=name):
            totals = Stat.of(row)
            whole.add(totals)
            points[span.index(row.at)].add(totals)
        return MetricSeries(
            kind=kind,
            name=name,
            period=period,
            start=span.start,
            end=span.end,
            step_seconds=span.step,
            totals=metric_row(kind, name, whole, whole.total_ms),
            points=[self._metric_point(span.moment(i), point) for i, point in enumerate(points)],
            slow_alert_id=self.repo.alert_of_route(name) if kind == PerfKind.HTTP else None,
        )

    @staticmethod
    def _metric_point(at: datetime, totals: Stat) -> MetricPoint:
        return MetricPoint(
            at=at,
            count=totals.count,
            errors=totals.errors,
            client_errors=totals.client_errors,
            avg_ms=totals.avg_ms,
            p50_ms=totals.quantile(0.5),
            p95_ms=totals.quantile(0.95),
            p99_ms=totals.quantile(0.99),
            max_ms=round(totals.max_ms, 1),
        )

    def web_vitals(self, period: str, page: PageParams) -> WebVitalsList:
        """Las pantallas de la aplicación web (la de más muestras primero) con el p75 de cada Web Vital y sus
        tareas largas: dos consultas para toda la página."""
        span = window(period)
        screens, total = self.repo.screens_page(span.grain, span.since, span.until, offset=page.offset, limit=page.size)
        found: dict[tuple[str, str], Stat] = {}
        if screens:
            for row in self.repo.screen_metrics(span.grain, screens, span.since, span.until):
                found[(str(row.kind), str(row.name))] = Stat.of(row)
        items = [self._screen(screen, found) for screen in screens]
        return WebVitalsList.of(items, total, page, period=period)

    @staticmethod
    def _screen(screen: str, found: dict[tuple[str, str], Stat]) -> ScreenVitals:
        vitals = {key: vital(spec, found.get((spec.kind.value, screen))) for key, spec in VITALS.items()}
        tasks = found.get((PerfKind.WEB_LONG_TASK.value, screen)) or Stat()
        return ScreenVitals(
            screen=screen,
            views=max((v.count for v in vitals.values() if v is not None), default=0),
            long_tasks=LongTasks(
                count=tasks.count,
                total_ms=round(tasks.total_ms, 1),
                p95_ms=tasks.quantile(0.95),
                max_ms=round(tasks.max_ms, 1),
            ),
            **vitals,
        )

    # ---------- Base de datos ----------

    def statements(self, sort: str, page: PageParams) -> StatementList:
        """Las consultas que más consumen la base (`pg_stat_statements`, texto normalizado sin parámetros)."""
        if not self.repo.statements_available():
            return StatementList.of([], 0, page, available=False, sort=sort)
        rows = self.repo.top_statements(sort, offset=page.offset, limit=page.size)
        total = int(rows[0].total_count) if rows else 0
        items = [
            Statement(
                query_id=str(row.query_id),
                query=str(row.query_text),
                calls=int(row.calls),
                total_ms=round(float(row.total_ms), 1),
                mean_ms=round(float(row.mean_ms), 2),
                max_ms=round(float(row.max_ms), 1),
                rows=int(row.row_count),
                share=_ratio(float(row.total_ms), float(row.grand_total_ms or 0)),
            )
            for row in rows
        ]
        return StatementList.of(items, total, page, available=True, sort=sort)

    # ---------- Alertas de peticiones lentas (regla 18) ----------

    def alerts(self, *, status: str | None, search: str | None, page: PageParams) -> SlowAlertList:
        as_of = datetime.now(UTC)
        rows, total = self.repo.alerts_page(status=status, search=search, offset=page.offset, limit=page.size)
        return SlowAlertList.of([self._alert(row) for row in rows], total, page, as_of=as_of)

    def summary(self) -> SlowAlertSummary:
        """El contador del menú y el aviso en vivo del ADMIN (la abierta que se abrió o reabrió más recientemente)."""
        counts = self.repo.alert_counts()
        latest = self.repo.latest_open()
        return SlowAlertSummary(
            open=counts.get(SlowAlertStatus.OPEN.value, 0),
            acknowledged=counts.get(SlowAlertStatus.ACKNOWLEDGED.value, 0),
            latest=LatestAlert(
                id=latest.id, route=latest.route, opened_at=as_utc(latest.opened_at), last_ms=round(latest.last_ms, 1)
            )
            if latest
            else None,
        )

    def alert(self, alert_id: int) -> SlowAlertDetail:
        """Una alerta con su muestra, sus peticiones y su p95 de las últimas 24 horas y el error de su traceId."""
        row = self._get(alert_id)
        now = datetime.now(UTC)
        since = (now - ALERT_WINDOW).replace(minute=0, second=0, microsecond=0)
        totals = Stat.of(self.repo.route_totals(HOURS, row.route, since, now))
        seen = as_utc(row.last_seen_at)
        error_report = (
            self.repo.error_report_of_trace(row.last_trace_id, seen - TRACE_WINDOW, seen + TRACE_WINDOW)
            if row.last_trace_id
            else None
        )
        return SlowAlertDetail(
            **self._alert(row).model_dump(),
            sample=json.loads(row.sample) if row.sample else None,
            status_changed_at=as_utc(row.status_changed_at),
            status_changed_by=row.status_changed_by,
            requests_24h=totals.count,
            p95_ms_24h=totals.quantile(0.95),
            error_report_id=error_report,
        )

    def set_status(self, alert_id: int, status: SlowAlertStatus, admin: User) -> SlowAlertDetail:
        """Cambia el seguimiento (abierta, en atención, resuelta); quién lo hizo queda como su correo."""
        current = self._get(alert_id)
        if current.status != status.value:
            self.repo.set_alert_status(alert_id, status.value, at=datetime.now(UTC), by=admin.email)
            self.db.commit()
            self.db.expire_all()
        return self.alert(alert_id)

    def _get(self, alert_id: int) -> SlowRequestAlert:
        row = self.repo.alert(alert_id)
        if row is None:
            raise NotFoundError(code="SLOW_ALERT_NOT_FOUND")
        return row

    @staticmethod
    def _alert(row: SlowRequestAlert) -> SlowAlert:
        return SlowAlert(
            id=row.id,
            route=row.route,
            method=row.method,
            path=row.path,
            status=SlowAlertStatus(row.status),
            count=row.count,
            avg_ms=round(row.total_ms / row.count, 1),
            last_ms=round(row.last_ms, 1),
            max_ms=round(row.max_ms, 1),
            threshold_ms=row.threshold_ms,
            first_seen_at=as_utc(row.first_seen_at),
            last_seen_at=as_utc(row.last_seen_at),
            opened_at=as_utc(row.opened_at),
            last_trace_id=row.last_trace_id,
            last_status=row.last_status,
            reopened=row.reopened,
        )
