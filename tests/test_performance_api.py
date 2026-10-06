"""Pantalla "Rendimiento" del ADMIN: el middleware mide cada petición sin consultas de más, las alertas de peticiones
lentas (regla 18) y las lecturas del ADMIN por periodo (resumen, rutas, funciones, navegador, base de datos), con
sus resúmenes por hora y por día y su depuración."""

import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.perf_meter import OTHER, PerfMeter, SlowRequestLog, perf_meter, slow_requests
from app.models import ErrorOccurrence, ErrorReport, PerfDay, PerfHour, PerfMinute, SlowAlertStatus, SlowRequestAlert
from app.repositories import performance_repository
from app.services import maintenance_service, perf_rollup, perf_store
from app.services.performance_service import window
from tests.conftest import ADMIN_EMAIL
from tests.test_performance import count_queries

ROUTE = "GET /api/employees/{employee_id}"
LOGIN = "POST /api/auth/login"
SCREEN = "/admin/companies/{id}"
URL = "/api/admin/performance"


def _now() -> datetime:
    return datetime.now(UTC)


def seed(now: datetime | None = None, *, slow: bool = True) -> datetime:
    """Rendimiento de varias rutas, funciones y pantallas en el último minuto (y lo que resume el mantenimiento)."""
    moment = now or _now()
    at = moment.timestamp() - 5
    meter, log = PerfMeter(1000), SlowRequestLog(100)
    for i in range(10):
        meter.record("HTTP", ROUTE, 10 + i, db_ms=2, db_queries=1, bytes_out=100, at=at)
    meter.record("HTTP", ROUTE, 1500, error=True, db_ms=500, db_queries=3, bytes_in=20, at=at)
    meter.record("HTTP", LOGIN, 300, client_error=True, at=at)
    meter.record("FUNCTION", "face.detect", 40, at=at)
    meter.record("FUNCTION", "face.detect", 60, error=True, at=at)
    meter.record("FUNCTION", "billing.issue_charges", 900, at=at)
    meter.record("WEB_API", ROUTE, 120, error=True, at=at)
    meter.record("WEB_LCP", SCREEN, 2100, at=at)
    meter.record("WEB_CLS", SCREEN, 150, at=at)
    meter.record("WEB_INP", SCREEN, 600, at=at)
    meter.record("WEB_LONG_TASK", SCREEN, 80, at=at)
    meter.record("WEB_FCP", "/login", 900, at=at)
    if slow:
        sample = {"method": "GET", "path": "/api/employees/7", "status": 500, "trace_id": "trace-slow"}
        log.record(ROUTE, 1500, trace_id="trace-slow", status=500, threshold_ms=1000, sample=sample, at=at)
        log.record(LOGIN, 1200, trace_id="trace-login", status=401, threshold_ms=1000, sample=None, at=at - 60)
    perf_store.flush(meter, log)
    with SessionLocal() as db:
        perf_rollup.run(db, moment)
    return moment


def _get(client, headers, path: str, **params) -> dict:
    response = client.get(f"{URL}{path}", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ---------------------------------------------------------------- observadores en el middleware


def test_every_request_is_measured_in_memory_without_extra_queries(client, company_headers):
    client.get("/api/users/me", headers=company_headers)  # calienta catálogos
    perf_meter.clear()
    with count_queries() as statements:
        response = client.get("/api/users/me", headers=company_headers)
    assert response.status_code == 200 and len(statements) <= 2  # solo la autenticación: medir no consulta nada
    client.get("/api/no-existe")
    client.post("/api/employees", json={}, headers=company_headers)
    stats = {name: stat for (_, kind, name), stat in perf_meter.drain().items() if kind == "HTTP"}
    me = stats["GET /api/users/me"]
    assert me.count == 1 and me.db_queries >= len(statements) and me.db_ms > 0
    assert me.bytes_out == len(response.content) and me.total_ms > 0 and sum(me.buckets) == 1
    assert stats[OTHER].client_errors == 1  # una URL que no existe no crea su propia llave
    assert stats["POST /api/employees"].client_errors == 1 and stats["POST /api/employees"].bytes_in == 2


def test_requests_slower_than_the_threshold_alert_the_admin_grouped_by_route(
    client, company_headers, monkeypatch, caplog
):
    """Regla 18: cada petición lenta suma a la alerta de su ruta (en memoria) con su traceId y una muestra sin
    secretos; ninguna consulta de más y una línea en el log."""
    client.get("/api/users/me", headers=company_headers)
    slow_requests.clear()
    monkeypatch.setattr(settings, "SLOW_REQUEST_THRESHOLD_MS", 0)
    with caplog.at_level(logging.WARNING, logger="app.access"), count_queries() as statements:
        response = client.get("/api/users/me?password=secreto&page=2", headers=company_headers)
    assert response.status_code == 200 and len(statements) <= 2
    trace_id = response.headers["X-Request-ID"]
    assert "Petición lenta GET /api/users/me" in caplog.text and trace_id in caplog.text
    client.get("/api/users/me", headers=company_headers)
    entry = slow_requests.drain()["GET /api/users/me"]
    assert entry.count == 2 and entry.threshold_ms == 0 and entry.last_status == 200
    sample = entry.sample or {}
    assert sample["user"]["role"] == "COMPANY" and sample["company_id"] and sample["db_queries"] >= 1
    assert sample["path"] == "/api/users/me" and sample["query"] is None  # la última no traía query
    monkeypatch.setattr(settings, "SLOW_REQUEST_THRESHOLD_MS", 0)
    client.get("/api/users/me?password=secreto&page=2", headers=company_headers)
    query = (slow_requests.drain()["GET /api/users/me"].sample or {})["query"]
    assert query["password"] != "secreto" and query["page"] == "2"  # el secreto nunca se guarda
    client.get("/api/no-existe")  # sin sesión: la muestra no lleva cuenta
    assert (slow_requests.drain()[OTHER].sample or {})["user"] is None


# ---------------------------------------------------------------- periodos


@pytest.mark.parametrize(
    ("period", "grain", "points", "step"),
    [
        ("1h", "minute", 60, 60),
        ("6h", "minute", 72, 300),
        ("24h", "hour", 24, 3600),
        ("7d", "hour", 56, 10800),
        ("30d", "day", 30, 86400),
        ("90d", "day", 90, 86400),
    ],
)
def test_each_period_reads_its_grain(period, grain, points, step):
    span = window(period, datetime(2026, 11, 2, 15, 30, 20, tzinfo=UTC))
    assert span.grain.time.key == grain and span.points == points and span.step == step
    assert span.index(span.since) == 0 and span.moment(1) == span.start + timedelta(seconds=step)


def test_day_periods_follow_the_business_day():
    span = window("30d", datetime(2026, 11, 2, 3, 0, tzinfo=UTC))  # 21:00 del 1 de noviembre en la hora del Centro
    assert span.until == date(2026, 11, 2) and span.since == date(2026, 10, 3)
    assert span.end == datetime(2026, 11, 2, 6, 0, tzinfo=UTC)  # medianoche del Centro = 06:00 UTC


# ---------------------------------------------------------------- lecturas del ADMIN


@pytest.mark.parametrize("period", ["1h", "6h", "24h", "7d", "30d", "90d"])
def test_the_overview_has_totals_series_and_the_slowest_routes_and_functions(client, admin_headers, period):
    seed()
    data = _get(client, admin_headers, "/overview", period=period)
    totals = data["totals"]
    assert (totals["requests"], totals["server_errors"], totals["client_errors"]) == (12, 1, 1)
    assert totals["error_rate"] == round(100 / 12, 2) and totals["p99_ms"] <= totals["max_ms"] == 1500
    assert totals["avg_queries"] == round(13 / 12, 2) and 0 < totals["db_share"] < 100 and totals["bytes_out"] == 1000
    assert sum(point["requests"] for point in data["points"]) == 12 and data["slow_threshold_ms"] == 1000
    assert data["slow_face_threshold_ms"] == 2500  # las rutas faciales (excepción de la regla 18)
    assert data["open_alerts"] == 2
    assert [row["name"] for row in data["top_routes"]] == [ROUTE, LOGIN]  # la más lenta (p95) primero
    assert [row["name"] for row in data["top_functions"]] == ["billing.issue_charges", "face.detect"]
    assert data["top_routes"][0]["avg_db_ms"] is not None and data["top_functions"][0]["avg_db_ms"] is None


def test_an_empty_platform_has_zeros(client, admin_headers):
    data = _get(client, admin_headers, "/overview", period="1h")
    assert data["totals"]["requests"] == 0 and data["totals"]["p95_ms"] == 0 and len(data["points"]) == 60
    assert data["top_routes"] == [] and data["open_alerts"] == 0


@pytest.mark.parametrize("sort", ["impact", "p95", "mean", "max", "count", "errors"])
def test_routes_and_functions_are_listed_with_their_percentiles(client, admin_headers, sort):
    seed()
    data = _get(client, admin_headers, "/metrics", kind="HTTP", period="1h", sort=sort, size=50)
    assert data["total"] == 2 and {row["name"] for row in data["items"]} == {ROUTE, LOGIN}
    assert (data["kind"], data["period"], data["sort"]) == ("HTTP", "1h", sort)
    employees = next(row for row in data["items"] if row["name"] == ROUTE)
    assert employees["count"] == 11 and employees["errors"] == 1 and employees["p50_ms"] < employees["p99_ms"]
    assert employees["avg_queries"] == round(13 / 11, 2) and employees["bytes_out"] == 1000
    assert round(sum(row["share"] for row in data["items"])) == 100


def test_metrics_search_paginate_and_cover_every_kind(client, admin_headers):
    seed()
    found = _get(client, admin_headers, "/metrics", kind="HTTP", period="24h", search="  EMPLOYEES ")
    assert [row["name"] for row in found["items"]] == [ROUTE]
    beyond = _get(client, admin_headers, "/metrics", kind="HTTP", period="24h", page=5, size=1)
    assert beyond["items"] == [] and beyond["total"] == 2  # más allá del final: se cuenta aparte
    assert _get(client, admin_headers, "/metrics", kind="HTTP", period="1h", search="nada")["total"] == 0
    functions = _get(client, admin_headers, "/metrics", kind="FUNCTION", period="1h", sort="errors")["items"]
    assert functions[0]["name"] == "face.detect" and functions[0]["error_rate"] == 50
    assert functions[0]["avg_queries"] is None and functions[0]["db_share"] is None
    browser = _get(client, admin_headers, "/metrics", kind="WEB_API", period="1h")["items"]
    assert browser[0]["name"] == ROUTE and browser[0]["errors"] == 1
    invalid = client.get(f"{URL}/metrics", params={"kind": "OTRO"}, headers=admin_headers)
    assert invalid.status_code == 422 and invalid.json()["errors"][0]["field"] == "kind"


def test_a_route_or_function_has_its_series_and_its_alert(client, admin_headers):
    seed()
    series = _get(client, admin_headers, "/metrics/series", kind="HTTP", name=ROUTE, period="6h")
    assert series["totals"]["count"] == 11 and len(series["points"]) == 72 and series["step_seconds"] == 300
    assert sum(point["count"] for point in series["points"]) == 11 and series["slow_alert_id"] is not None
    function = _get(client, admin_headers, "/metrics/series", kind="FUNCTION", name="face.detect", period="24h")
    assert function["totals"]["errors"] == 1 and function["slow_alert_id"] is None
    nothing = _get(client, admin_headers, "/metrics/series", kind="HTTP", name="GET /api/nada", period="1h")
    assert nothing["totals"]["count"] == 0 and nothing["totals"]["avg_db_ms"] == 0 and nothing["slow_alert_id"] is None


def test_web_vitals_per_screen_with_googles_ratings(client, admin_headers):
    seed()
    data = _get(client, admin_headers, "/web-vitals", period="1h")
    assert data["total"] == 2 and data["period"] == "1h"
    admin, login = data["items"]
    assert admin["screen"] == SCREEN and admin["views"] == 1
    assert (admin["lcp"]["rating"], admin["lcp"]["unit"], admin["lcp"]["good"]) == ("GOOD", "ms", 2500)
    assert admin["cls"] == {**admin["cls"], "unit": "score", "rating": "NEEDS_IMPROVEMENT", "p75": 0.138}
    assert admin["inp"]["rating"] == "POOR" and admin["fcp"] is None and admin["ttfb"] is None
    assert admin["long_tasks"] == {"count": 1, "total_ms": 80.0, "p95_ms": 79.8, "max_ms": 80.0}
    assert login["screen"] == "/login" and login["fcp"]["rating"] == "GOOD" and login["long_tasks"]["count"] == 0
    beyond = _get(client, admin_headers, "/web-vitals", period="1h", page=3, size=1)
    assert beyond["items"] == [] and beyond["total"] == 2
    empty = _get(client, admin_headers, "/web-vitals", period="7d", page=1)
    assert empty["total"] == 2 and empty["items"][0]["screen"] == SCREEN  # también por hora (resumen)


def test_statements_without_pg_stat_statements_say_so(client, admin_headers, monkeypatch):
    monkeypatch.setattr(performance_repository.PerformanceRepository, "statements_available", lambda _self: False)
    data = _get(client, admin_headers, "/statements")
    assert data["available"] is False and data["items"] == [] and data["sort"] == "total"


def test_statements_come_normalized_from_pg_stat_statements(client, admin_headers, monkeypatch):
    rows = [
        SimpleNamespace(
            query_id=-9_000_000_000_000_000_001,
            query_text="SELECT * FROM workforce.employees WHERE company_id = $1",
            calls=10,
            total_ms=30.0,
            mean_ms=3.0,
            max_ms=9.5,
            row_count=40,
            total_count=2,
            grand_total_ms=40.0,
        )
    ]
    monkeypatch.setattr(performance_repository.PerformanceRepository, "statements_available", lambda _self: True)
    monkeypatch.setattr(performance_repository.PerformanceRepository, "top_statements", lambda *_a, **_k: rows)
    data = _get(client, admin_headers, "/statements", sort="mean")
    assert data["available"] is True and data["total"] == 2 and data["sort"] == "mean"
    assert data["items"][0] == {
        "query_id": "-9000000000000000001",
        "query": "SELECT * FROM workforce.employees WHERE company_id = $1",
        "calls": 10,
        "total_ms": 30.0,
        "mean_ms": 3.0,
        "max_ms": 9.5,
        "rows": 40,
        "share": 75.0,
    }
    monkeypatch.setattr(performance_repository.PerformanceRepository, "top_statements", lambda *_a, **_k: [])
    assert _get(client, admin_headers, "/statements", page=9)["total"] == 0


def test_the_statements_repository_reads_the_function_only_in_postgresql():
    """Con SQLite no hay `pg_stat_statements`; con PostgreSQL depende de que el servidor cargue la extensión y, si la
    tiene, la función SECURITY DEFINER responde (con el rol de la plataforma)."""
    with SessionLocal() as db:
        repo = performance_repository.PerformanceRepository(db)
        available = repo.statements_available()
        assert available is False or db.get_bind().dialect.name == "postgresql"
        if available:
            assert isinstance(repo.top_statements("calls", offset=0, limit=5), list)


def test_the_statements_repository_asks_postgresql_through_its_function():
    """En PostgreSQL: ¿existe la vista? y la función SECURITY DEFINER con el orden y la página."""
    executed: list = []

    class _Db:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def scalar(self, statement):
            executed.append(str(statement))
            return True

        def execute(self, statement, params):
            executed.append((str(statement), params))
            return SimpleNamespace(all=lambda: ["fila"])

    repo = performance_repository.PerformanceRepository(_Db())  # type: ignore[arg-type]
    assert repo.statements_available() is True
    assert repo.top_statements("calls", offset=10, limit=5) == ["fila"]
    assert "pg_stat_statements" in executed[0] and "ops.top_statements" in executed[1][0]
    assert executed[1][1] == {"sort": "calls", "limit": 5, "offset": 10}


# ---------------------------------------------------------------- alertas de peticiones lentas


def _link_error(trace_id: str, at: datetime) -> int:
    with SessionLocal() as db:
        report = ErrorReport(
            fingerprint="f-slow",
            source="HTTP",
            severity="ERROR",
            code="DATABASE_TIMEOUT",
            message="La base tardó",
            first_seen_at=at,
            last_seen_at=at,
        )
        db.add(report)
        db.flush()
        db.add(ErrorOccurrence(report_id=report.id, occurred_at=at, trace_id=trace_id, message="La base tardó"))
        db.commit()
        return report.id


def test_the_alert_inbox_lists_filters_and_summarizes(client, admin_headers):
    seed()
    inbox = _get(client, admin_headers, "/alerts")
    assert inbox["total"] == 2 and [a["route"] for a in inbox["items"]] == [ROUTE, LOGIN]  # lo más reciente primero
    first = inbox["items"][0]
    assert (first["status"], first["count"], first["avg_ms"], first["last_trace_id"]) == (
        "OPEN",
        1,
        1500.0,
        "trace-slow",
    )
    assert (first["method"], first["path"], first["last_status"], first["threshold_ms"]) == (
        "GET",
        ROUTE[4:],
        500,
        1000,
    )
    assert inbox["as_of"]
    assert _get(client, admin_headers, "/alerts", search="login")["total"] == 1
    assert _get(client, admin_headers, "/alerts", status="RESOLVED")["total"] == 0
    summary = _get(client, admin_headers, "/alerts/summary")
    assert (summary["open"], summary["acknowledged"]) == (2, 0)
    assert summary["latest"]["route"] == ROUTE and summary["latest"]["last_ms"] == 1500


def test_an_alert_has_its_sample_its_last_day_and_its_error(client, admin_headers):
    moment = seed()
    alert_id = _get(client, admin_headers, "/alerts", search="employees")["items"][0]["id"]
    report_id = _link_error("trace-slow", moment)
    detail = _get(client, admin_headers, f"/alerts/{alert_id}")
    assert detail["sample"]["path"] == "/api/employees/7" and detail["error_report_id"] == report_id
    assert detail["requests_24h"] == 11 and detail["p95_ms_24h"] > 0 and detail["status_changed_by"] is None
    login_id = _get(client, admin_headers, "/alerts", search="login")["items"][0]["id"]
    login = _get(client, admin_headers, f"/alerts/{login_id}")
    assert login["sample"] is None and login["error_report_id"] is None
    missing = client.get(f"{URL}/alerts/999999", headers=admin_headers)
    assert missing.status_code == 404 and missing.json()["code"] == "SLOW_ALERT_NOT_FOUND"


def test_an_alert_without_a_trace_id_has_no_error(client, admin_headers):
    log = SlowRequestLog(10)
    log.record(ROUTE, 1500, trace_id=None, status=200, threshold_ms=1000, sample=None)
    perf_store.flush(PerfMeter(10), log)
    alert_id = _get(client, admin_headers, "/alerts")["items"][0]["id"]
    assert _get(client, admin_headers, f"/alerts/{alert_id}")["error_report_id"] is None


def test_the_admin_follows_an_alert_up(client, admin_headers):
    seed()
    alert_id = _get(client, admin_headers, "/alerts", search="employees")["items"][0]["id"]
    url = f"{URL}/alerts/{alert_id}"
    acknowledged = client.patch(url, json={"status": "ACKNOWLEDGED"}, headers=admin_headers)
    assert acknowledged.status_code == 200 and acknowledged.json()["code"] == "SLOW_ALERT_STATUS_UPDATED"
    data = acknowledged.json()["data"]
    assert data["status"] == "ACKNOWLEDGED" and data["status_changed_by"] == ADMIN_EMAIL
    changed_at = data["status_changed_at"]
    again = client.patch(url, json={"status": "ACKNOWLEDGED"}, headers=admin_headers).json()["data"]
    assert again["status_changed_at"] == changed_at  # sin cambios: no se toca
    assert _get(client, admin_headers, "/alerts/summary")["acknowledged"] == 1
    resolved = client.patch(url, json={"status": "RESOLVED"}, headers=admin_headers).json()["data"]
    assert resolved["status"] == "RESOLVED"
    reopened = client.patch(url, json={"status": "OPEN"}, headers=admin_headers).json()["data"]
    assert reopened["status"] == "OPEN" and reopened["opened_at"] > resolved["opened_at"]  # reabrirla a mano la abre
    bad = client.patch(url, json={"status": "BORRADA"}, headers=admin_headers)
    assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "status"
    extra = client.patch(url, json={"status": "OPEN", "nota": "x"}, headers=admin_headers)
    assert extra.status_code == 422
    gone = client.patch(f"{URL}/alerts/999999", json={"status": "OPEN"}, headers=admin_headers)
    assert gone.status_code == 404


def test_no_open_alert_means_no_live_notice(client, admin_headers):
    seed(slow=False)
    assert _get(client, admin_headers, "/alerts/summary") == {"open": 0, "acknowledged": 0, "latest": None}


# ---------------------------------------------------------------- resúmenes y depuración (mantenimiento)


def test_maintenance_rolls_up_hours_and_days_idempotently():
    now = _now()
    seed(now)
    with SessionLocal() as db:
        assert perf_rollup.run(db, now) > 0  # repetirlo no duplica (reemplaza las mismas filas)
        hours = {(row.kind, row.name): row for row in db.scalars(select(PerfHour))}
        days = {(row.kind, row.name): row for row in db.scalars(select(PerfDay))}
    assert hours[("HTTP", ROUTE)].count == 11 and days[("HTTP", ROUTE)].count == 11
    assert hours[("HTTP", ROUTE)].max_ms == 1500 and days[("WEB_CLS", SCREEN)].h_150 == 1


def test_maintenance_purges_old_performance_and_old_resolved_alerts():
    now = _now()
    old = now - timedelta(days=settings.PERF_DAY_RETENTION_DAYS + 5)
    with SessionLocal() as db:
        db.add_all(
            [
                PerfMinute(kind="HTTP", minute=now - timedelta(days=settings.PERF_MINUTE_RETENTION_DAYS + 1), name="a"),
                PerfMinute(kind="HTTP", minute=now - timedelta(minutes=2), name="a"),
                PerfHour(kind="FUNCTION", hour=now - timedelta(days=settings.PERF_HOUR_RETENTION_DAYS + 1), name="b"),
                PerfDay(kind="WEB_LCP", day=old.date(), name="/x"),
                SlowRequestAlert(
                    route="GET /api/viejo",
                    method="GET",
                    path="/api/viejo",
                    status=SlowAlertStatus.RESOLVED,
                    count=1,
                    total_ms=1500,
                    last_ms=1500,
                    max_ms=1500,
                    threshold_ms=1000,
                    first_seen_at=old,
                    last_seen_at=old,
                    opened_at=old,
                ),
            ]
        )
        db.commit()
        removed = maintenance_service.purge_expired(db, now=now)
    assert removed["rendimiento por minuto"] == 1 and removed["rendimiento por hora"] == 1
    assert removed["rendimiento por día"] == 1 and removed["alertas de peticiones lentas resueltas"] == 1
    assert removed[maintenance_service.PERF_ROLLUPS] > 0
    with SessionLocal() as db:
        assert db.query(PerfMinute).count() == 1 and db.query(SlowRequestAlert).count() == 0
