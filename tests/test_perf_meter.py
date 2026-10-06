"""Observadores de rendimiento del backend: histograma de cubetas fijas, acumuladores en memoria (acotados), funciones
medidas (`observed`), tiempo de BD por petición, la espera de una conexión y el guardado en lotes (UPSERT que suma,
nada se pierde si la BD falla, alertas de peticiones lentas agrupadas que se reabren solas)."""

import json
import logging
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from app.core import observability
from app.core.clock import as_utc
from app.core.database import SessionLocal
from app.core.histogram import BOUNDS_MS, BUCKETS, COLUMNS, bucket_of, empty, percentile
from app.core.observability import RequestTimer, TimedQueuePool, observed, request_timer_var
from app.core.perf_meter import OTHER, PerfMeter, SlowRequestLog, Stat, minute_of, perf_meter
from app.models import PerfMinute, SlowAlertStatus, SlowRequestAlert
from app.services import perf_store
from app.services.perf_store import PerfFlusher

#: Un instante fijo (lunes 2 de noviembre de 2026, 15:30:20 UTC).
AT = datetime(2026, 11, 2, 15, 30, 20, tzinfo=UTC).timestamp()
WAIT_SECONDS = 5


def _minutes():
    with SessionLocal() as db:
        return {(row.kind, row.name): row for row in db.scalars(select(PerfMinute))}


def _alerts():
    with SessionLocal() as db:
        return {row.route: row for row in db.scalars(select(SlowRequestAlert))}


# ---------------------------------------------------------------- histograma


def test_each_time_falls_in_the_first_bucket_that_covers_it():
    assert len(COLUMNS) == BUCKETS == len(BOUNDS_MS) + 1 and COLUMNS[-1] == "h_inf"
    assert [bucket_of(ms) for ms in (0, 1, 1.01, 5, 999.9, 1000, 1000.5, 10_000, 10_001)] == [
        0,
        0,
        1,
        2,
        13,
        13,
        14,
        16,
        17,
    ]
    assert empty() == [0] * BUCKETS


def test_percentiles_interpolate_inside_their_bucket_and_never_pass_the_maximum():
    counts = empty()
    counts[bucket_of(7)] = 50  # (5, 10]
    counts[bucket_of(90)] = 45  # (75, 100]
    counts[bucket_of(1500)] = 5  # (1000, 2000]
    assert percentile(counts, 0.5, 1800) == 10.0  # el 50 % termina justo en la cubeta de 10 ms
    assert percentile(counts, 0.25, 1800) == 7.5
    assert percentile(counts, 0.95, 1800) == 100.0
    assert percentile(counts, 0.99, 1800) == 1640.0  # 4 de 5 dentro de (1000, 1800]: recortada al máximo real
    assert percentile(counts, 1.0, 1800) == 1800.0
    overflow = empty()
    overflow[-1] = 2  # más de 10 s: la última cubeta termina en el máximo
    assert percentile(overflow, 0.5, 30_000) == 20_000.0
    assert percentile(empty(), 0.95, 0) == 0.0  # sin datos
    assert percentile([], 0.95, 0) == 0.0
    zero = empty()
    zero[0] = 3  # todo en 0 ms (máximo 0): la cubeta no se recorta
    assert percentile(zero, 0.5, 0) == 0.5
    assert percentile(counts, 0, 1800) == 0.0  # el percentil 0 es el inicio de la primera cubeta
    assert percentile(counts, 7, 1800) == 1800.0  # fuera de rango: se acota


# ---------------------------------------------------------------- acumuladores


def test_measurements_add_up_per_minute_kind_and_name():
    meter = PerfMeter(100)
    meter.record("HTTP", "GET /api/x", 12, db_ms=3, db_queries=2, bytes_in=10, bytes_out=100, at=AT)
    meter.record("HTTP", "GET /api/x", 1500, error=True, at=AT + 5)
    meter.record("HTTP", "GET /api/x", 4, client_error=True, at=AT + 60)  # otro minuto
    meter.record("FUNCTION", "face.detect", -3, at=AT)  # un reloj que retrocede cuenta 0
    stats = meter.drain()
    assert meter.pending() == 0 and len(stats) == 3
    first = stats[(minute_of(AT), "HTTP", "GET /api/x")]
    assert (first.count, first.errors, first.client_errors, first.max_ms) == (2, 1, 0, 1500)
    assert (first.total_ms, first.db_ms, first.db_queries, first.bytes_in, first.bytes_out) == (1512, 3, 2, 10, 100)
    assert first.buckets[bucket_of(12)] == 1 and first.buckets[bucket_of(1500)] == 1
    row = first.row()
    assert row["h_20"] == 1 and row["h_2000"] == 1 and row["count"] == 2
    assert stats[(minute_of(AT), "FUNCTION", "face.detect")].buckets[0] == 1


def test_memory_is_bounded_and_nothing_is_lost():
    meter = PerfMeter(2)
    meter.record("HTTP", "GET /api/a", 1, at=AT)
    meter.record("HTTP", "GET /api/b", 1, at=AT)
    meter.record("HTTP", "GET /api/c", 1, at=AT)  # ya no cabe: OTHER de su minuto y tipo
    meter.record("HTTP", "GET /api/a", 1, at=AT)  # la que ya existía sigue sumando
    assert meter.folded == 1 and meter.pending() == 3
    stats = meter.drain()
    assert stats[(minute_of(AT), "HTTP", OTHER)].count == 1 and stats[(minute_of(AT), "HTTP", "GET /api/a")].count == 2
    # Lo que vuelve tras una falla respeta el mismo tope.
    meter.record("HTTP", "GET /api/z", 1, at=AT)
    meter.record("HTTP", "GET /api/y", 1, at=AT)
    meter.restore(stats)
    restored = meter.drain()
    assert restored[(minute_of(AT), "HTTP", OTHER)].count == 4  # a (2), b y c no cupieron junto a z y y
    meter.clear()
    assert meter.folded == 0 and meter.pending() == 0


def test_a_disabled_meter_records_nothing_and_names_are_clipped():
    disabled = PerfMeter(10, enabled=False)
    disabled.record("HTTP", "GET /api/x", 1)
    assert disabled.pending() == 0
    meter = PerfMeter(10)
    meter.record("FUNCTION", "x" * 500, 1)
    ((_, _, name),) = meter.drain()
    assert len(name) == 160


def test_stats_merge_keeps_the_maximum():
    one, other = Stat(count=1, total_ms=5, max_ms=5), Stat(count=2, total_ms=50, max_ms=40)
    other.buckets[3] = 2
    one.add(other)
    assert (one.count, one.total_ms, one.max_ms, one.buckets[3]) == (3, 55, 40, 2)


def test_slow_requests_group_by_route_with_the_latest_sample():
    log = SlowRequestLog(2)
    log.record("GET /api/a", 1500, trace_id="t1", status=200, threshold_ms=1000, sample={"n": 1}, at=AT)
    log.record("GET /api/a", 3000, trace_id="t2", status=503, threshold_ms=1000, sample={"n": 2}, at=AT + 10)
    log.record("GET /api/b", 1200, trace_id="t3", status=200, threshold_ms=1000, sample=None)
    log.record("GET /api/c", 1100, trace_id="t4", status=200, threshold_ms=1000, sample=None)  # no cabe: OTHER
    assert log.pending() == 3 and log.folded == 1
    routes = log.drain()
    first = routes["GET /api/a"]
    assert (first.count, first.total_ms, first.max_ms, first.last_ms, first.last_trace_id) == (
        2,
        4500,
        3000,
        3000,
        "t2",
    )
    assert (first.last_status, first.sample, first.first_at) == (503, {"n": 2}, AT)
    # Un grupo más viejo que llega después (lo que volvió de un lote fallido) no reemplaza la última ocurrencia.
    log.restore({"GET /api/a": first})
    older = SlowRequestLog(5)
    older.record("GET /api/a", 9000, trace_id="viejo", status=500, threshold_ms=1000, sample=None, at=AT - 100)
    log.restore(older.drain())
    merged = log.drain()["GET /api/a"]
    assert (merged.count, merged.max_ms, merged.last_trace_id, merged.first_at) == (3, 9000, "t2", AT - 100)
    log.record("GET /api/a", 1001, trace_id="t5", status=200, threshold_ms=1000, sample=None)
    log.clear()
    assert log.pending() == 0 and log.folded == 0


# ---------------------------------------------------------------- funciones medidas y tiempo de BD


def test_observed_measures_functions_and_blocks_and_counts_failures():
    perf_meter.clear()

    @observed("test.decorated")
    def work(value: int) -> int:
        return value * 2

    assert work(21) == 42 and work.__name__ == "work"
    with observed("test.block") as span:
        assert span.name == "test.block"
    with pytest.raises(ValueError), observed("test.block"):
        raise ValueError("falla de la función")
    stats = {name: stat for (_, kind, name), stat in perf_meter.drain().items() if kind == "FUNCTION"}
    assert stats["test.decorated"].count == 1 and stats["test.decorated"].errors == 0
    assert stats["test.block"].count == 2 and stats["test.block"].errors == 1


def test_measuring_never_breaks_what_it_measures(monkeypatch, caplog):
    """Si el acumulador fallara, la función medida (p. ej. pedir una conexión de la base) sigue igual y la falla del
    medidor queda registrada."""

    def broken(*_args, **_kwargs):
        raise RuntimeError("medidor roto")

    monkeypatch.setattr(perf_meter, "record", broken)

    @observed("test.resilient")
    def work() -> str:
        return "hecho"

    with caplog.at_level(logging.ERROR, logger="app.performance"):
        assert work() == "hecho"
    assert "No se pudo medir test.resilient" in caplog.text


def test_each_request_adds_its_database_time_and_statements():
    engine = create_engine("sqlite://")
    timer = RequestTimer()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))  # fuera de una petición: nada que sumar
        token = request_timer_var.set(timer)
        try:
            conn.execute(text("SELECT 1"))
            conn.execute(text("SELECT 2"))
        finally:
            request_timer_var.reset(token)
        conn.execute(text("SELECT 3"))
    assert timer.db_queries == 2 and timer.db_ms > 0
    # Una sentencia que empezó dentro de la petición y terminó fuera (o al revés) no se cuenta.
    with engine.connect() as conn:
        conn.info[observability._STARTED] = 1.0
        conn.execute(text("SELECT 4"))
    engine.dispose()


def test_waiting_for_a_database_connection_is_measured():
    perf_meter.clear()
    engine = create_engine("sqlite://", poolclass=TimedQueuePool)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    engine.dispose()
    stats = {name: stat for (_, _, name), stat in perf_meter.drain().items()}
    assert stats["db.acquire"].count >= 1


# ---------------------------------------------------------------- guardado en lotes


def _two_flushes() -> None:
    meter = PerfMeter(100)
    meter.record("HTTP", "GET /api/x", 20, db_ms=4, db_queries=2, bytes_in=5, bytes_out=50, at=AT)
    meter.record("HTTP", "GET /api/x", 600, error=True, at=AT)
    assert perf_store.flush(meter, SlowRequestLog(10)) == 1
    # Otro lote (otra réplica) del mismo minuto suma sobre la misma fila; el máximo se queda con el mayor.
    meter.record("HTTP", "GET /api/x", 300, client_error=True, at=AT)
    assert perf_store.flush(meter, SlowRequestLog(10)) == 1


def test_batches_add_up_atomically_on_the_same_minute():
    _two_flushes()
    row = _minutes()[("HTTP", "GET /api/x")]
    assert (row.count, row.errors, row.client_errors, row.max_ms, row.total_ms) == (3, 1, 1, 600, 920)
    assert (row.db_ms, row.db_queries, row.bytes_in, row.bytes_out) == (4, 2, 5, 50)
    assert (row.h_20, row.h_400, row.h_600) == (1, 1, 1)
    assert perf_store.flush(PerfMeter(10), SlowRequestLog(10)) == 0  # nada pendiente: ninguna escritura


def test_slow_requests_are_one_row_per_route_and_reopen_when_they_recur():
    log = SlowRequestLog(10)
    log.record("GET /api/x/{id}", 1500, trace_id="t1", status=200, threshold_ms=1000, sample={"a": 1}, at=AT)
    log.record("GET /api/x/{id}", 2500, trace_id="t2", status=200, threshold_ms=1000, sample={"a": 2}, at=AT + 1)
    log.record("OTHER", 1200, trace_id="t3", status=404, threshold_ms=1000, sample=None, at=AT)
    assert perf_store.flush(PerfMeter(10), log) == 2
    alerts = _alerts()
    alert = alerts["GET /api/x/{id}"]
    assert (alert.method, alert.path, alert.status, alert.count, alert.max_ms) == (
        "GET",
        "/api/x/{id}",
        "OPEN",
        2,
        2500,
    )
    assert (alert.last_trace_id, json.loads(alert.sample or "{}"), alert.reopened) == ("t2", {"a": 2}, 0)
    assert (alerts["OTHER"].method, alerts["OTHER"].path, alerts["OTHER"].sample) == ("", "OTHER", None)
    # El ADMIN la atiende: si vuelve a ocurrir, sigue en atención (no se reabre).
    with SessionLocal() as db:
        db.get(SlowRequestAlert, alert.id).status = SlowAlertStatus.ACKNOWLEDGED
        db.commit()
    log.record("GET /api/x/{id}", 1100, trace_id="t4", status=200, threshold_ms=900, sample=None, at=AT + 50)
    perf_store.flush(PerfMeter(10), log)
    again = _alerts()["GET /api/x/{id}"]
    assert (again.status, again.count, again.last_ms, again.threshold_ms, again.reopened) == (
        "ACKNOWLEDGED",
        3,
        1100,
        900,
        0,
    )
    # Resuelta y vuelve a pasar: se reabre sola, cuenta la reapertura y su aviso (opened_at) es el de ahora.
    with SessionLocal() as db:
        row = db.get(SlowRequestAlert, alert.id)
        row.status, row.status_changed_by = SlowAlertStatus.RESOLVED, "admin@x.com"
        db.commit()
    log.record("GET /api/x/{id}", 1300, trace_id="t5", status=200, threshold_ms=900, sample=None, at=AT + 100)
    perf_store.flush(PerfMeter(10), log)
    reopened = _alerts()["GET /api/x/{id}"]
    assert (reopened.status, reopened.reopened, reopened.count, reopened.status_changed_by) == ("OPEN", 1, 4, None)
    assert as_utc(reopened.opened_at).timestamp() == AT + 100
    # Una ocurrencia más vieja que llega tarde (lote fallido de otra réplica) no reemplaza la última ni el inicio.
    log.record("GET /api/x/{id}", 1050, trace_id="tarde", status=200, threshold_ms=900, sample=None, at=AT - 500)
    perf_store.flush(PerfMeter(10), log)
    late = _alerts()["GET /api/x/{id}"]
    assert (late.last_trace_id, late.count) == ("t5", 5)
    assert as_utc(late.first_seen_at).timestamp() == AT - 500


def test_a_failed_flush_keeps_everything_for_the_next_round(monkeypatch, caplog):
    meter, log = PerfMeter(100), SlowRequestLog(10)
    meter.record("HTTP", "GET /api/x", 10, at=AT)
    log.record("GET /api/x", 1500, trace_id="t1", status=200, threshold_ms=1000, sample=None, at=AT)
    real_minutes, real_slow = perf_store.PerformanceRepository.add_minutes, perf_store.PerformanceRepository.add_slow

    def down(*_args):
        raise OperationalError("INSERT", {}, Exception("BD caída"))

    monkeypatch.setattr(perf_store.PerformanceRepository, "add_minutes", down)
    monkeypatch.setattr(perf_store.PerformanceRepository, "add_slow", down)
    with caplog.at_level(logging.ERROR, logger="app.performance"):
        assert perf_store.flush(meter, log) == 0
    assert "No se pudo guardar el rendimiento por minuto" in caplog.text
    assert "No se pudieron guardar las peticiones lentas" in caplog.text
    assert meter.pending() == 1 and log.pending() == 1
    meter.record("HTTP", "GET /api/x", 30, at=AT)
    monkeypatch.setattr(perf_store.PerformanceRepository, "add_minutes", real_minutes)
    monkeypatch.setattr(perf_store.PerformanceRepository, "add_slow", real_slow)
    assert perf_store.flush(meter, log) == 2
    assert _minutes()[("HTTP", "GET /api/x")].count == 2 and _alerts()["GET /api/x"].count == 1


def test_the_flusher_saves_periodically_survives_failures_and_flushes_on_stop(monkeypatch, caplog):
    rounds = {"n": 0}
    recovered = threading.Event()
    real = perf_store.flush

    def flaky(*args):
        rounds["n"] += 1
        if rounds["n"] == 1:
            raise RuntimeError("falla inesperada")
        recovered.set()
        return real(*args)

    monkeypatch.setattr(perf_store, "flush", flaky)
    flusher = PerfFlusher(0.01)
    with caplog.at_level(logging.ERROR, logger="app.performance"):
        flusher.start()
        try:
            assert recovered.wait(WAIT_SECONDS)
        finally:
            flusher.stop()
    assert "Falló el guardado periódico del rendimiento" in caplog.text and not flusher._thread.is_alive()
    monkeypatch.setattr(perf_store, "flush", real)
    perf_meter.clear()
    stopper = PerfFlusher(3600)
    stopper.start()
    perf_meter.record("FUNCTION", "test.on_stop", 5)
    stopper.stop()  # al apagar se guarda lo pendiente
    assert ("FUNCTION", "test.on_stop") in _minutes()
