"""Medidor de consumo: cada petición suma en memoria (sin tocar la BD) y un hilo guarda en lotes con
UPSERT que suman; memoria acotada, nada se pierde si la BD falla y el hilo nunca muere."""

import logging
import threading
from datetime import date

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import UsageDaily, UsageRoute, UsageUser
from app.services import usage_meter as meter_module
from app.services.usage_meter import OTHER_ROUTE, UsageFlusher, UsageMeter, tally_of, usage_meter
from tests.test_performance import count_queries

DAY = date(2026, 11, 3)
WAIT_SECONDS = 5


def _record(meter: UsageMeter, *, company=1, user=7, route="GET /api/x", status=200, ms=10.0, day=DAY) -> None:
    meter.record(
        company_id=company,
        user_id=user,
        route=route,
        bytes_in=100,
        bytes_out=1000,
        duration_ms=ms,
        status=status,
        day=day,
    )


def _rows(model):
    with SessionLocal() as db:
        return list(db.scalars(select(model)))


def test_requests_add_up_in_memory_and_flush_with_atomic_sums():
    meter = UsageMeter(1000)
    _record(meter, ms=10)
    _record(meter, ms=30, status=503)
    _record(meter, user=8, route="POST /api/y", status=422)
    _record(meter, company=None, user=None, route=OTHER_ROUTE)  # sin empresa ni cuenta: 0
    assert meter.pending() == 2 + 3 + 3  # empresas/día, rutas, cuentas
    assert meter.flush() == 8 and meter.pending() == 0 and meter.flush() == 0
    daily = {row.company_id: row for row in _rows(UsageDaily)}
    assert (daily[1].requests, daily[1].bytes_in, daily[1].bytes_out) == (3, 300, 3000)
    assert (daily[1].duration_ms, daily[1].max_ms, daily[1].server_errors, daily[1].client_errors) == (50, 30, 1, 1)
    assert daily[0].requests == 1
    # Otro lote (otra instancia) suma sobre las mismas filas; el máximo se queda con el mayor.
    _record(meter, ms=5)
    meter.flush()
    daily = {row.company_id: row for row in _rows(UsageDaily)}
    assert (daily[1].requests, daily[1].max_ms, daily[1].duration_ms) == (4, 30, 55)
    routes = {(r.company_id, r.route): r.requests for r in _rows(UsageRoute)}
    assert routes == {(1, "GET /api/x"): 3, (1, "POST /api/y"): 1, (0, OTHER_ROUTE): 1}
    users = {(u.company_id, u.user_id): u.requests for u in _rows(UsageUser)}
    assert users == {(1, 7): 3, (1, 8): 1, (0, 0): 1}


def test_memory_is_bounded_and_nothing_is_lost():
    meter = UsageMeter(3)
    _record(meter)
    _record(meter, route="GET /api/a", user=2)  # ya no cabe: va a OTHER y al usuario 0 de su empresa
    _record(meter, route="GET /api/a", user=2)
    assert meter.folded == 4 and meter.pending() == 5
    meter.flush()
    assert {r.route: r.requests for r in _rows(UsageRoute)} == {"GET /api/x": 1, OTHER_ROUTE: 2}
    assert sum(r.requests for r in _rows(UsageDaily)) == 3  # el total de la empresa siempre completo
    meter.clear()
    assert meter.folded == 0


def test_a_failed_flush_keeps_the_counters_for_the_next_round(monkeypatch, caplog):
    meter = UsageMeter(3)
    _record(meter, route="GET /api/a", user=1)
    real = meter_module.UsageRepository.add_counts

    def broken(self, model, rows):
        raise RuntimeError("BD caída")

    monkeypatch.setattr(meter_module.UsageRepository, "add_counts", broken)
    with caplog.at_level(logging.ERROR, logger="app.usage"):
        assert meter.flush() == 0
    assert any("No se pudo guardar el consumo" in r.getMessage() for r in caplog.records)
    # Mientras tanto llegó más (y ya no cabe el desglose nuevo): todo se suma al reintentar.
    _record(meter, route="GET /api/b", user=2)
    _record(meter, route="GET /api/c", user=3, company=2)
    monkeypatch.setattr(meter_module.UsageRepository, "add_counts", real)
    meter.flush()
    assert {(r.company_id, r.requests) for r in _rows(UsageDaily)} == {(1, 2), (2, 1)}
    assert sum(r.requests for r in _rows(UsageRoute)) == 3 and sum(r.requests for r in _rows(UsageUser)) == 3
    # Lo que no cupo al restaurar va a OTHER / usuario 0.
    failing = UsageMeter(3)
    _record(failing, route="GET /api/a", user=1)
    monkeypatch.setattr(meter_module.UsageRepository, "add_counts", broken)
    failing.flush()
    _record(failing, route="GET /api/z", user=9, company=5)
    failing._restore(
        meter_module.Batch({}, {(5, DAY, "GET /api/q"): tally_of(1, 1, 1, 200)}, {(5, DAY, 4): tally_of(1, 1, 1, 200)})
    )
    assert (5, DAY, OTHER_ROUTE) in failing._batch.routes and (5, DAY, 0) in failing._batch.users


def test_a_disabled_meter_records_nothing():
    meter = UsageMeter(10, enabled=False)
    _record(meter)
    assert meter.pending() == 0


def test_the_flusher_saves_periodically_survives_failures_and_flushes_on_stop(caplog):
    class Flaky(UsageMeter):
        def __init__(self) -> None:
            super().__init__(100)
            self.rounds = 0
            self.recovered = threading.Event()

        def flush(self) -> int:
            self.rounds += 1
            if self.rounds == 1:
                raise RuntimeError("falla inesperada")
            saved = super().flush()
            if saved:
                self.recovered.set()
            return saved

    meter = Flaky()
    _record(meter)
    flusher = UsageFlusher(0.01, meter)
    with caplog.at_level(logging.ERROR, logger="app.usage"):
        flusher.start()
        try:
            assert meter.recovered.wait(WAIT_SECONDS)
        finally:
            flusher.stop()
    assert any("guardado periódico del consumo" in r.getMessage() for r in caplog.records)
    last = UsageMeter(100)
    stopper = UsageFlusher(3600, last)
    stopper.start()
    _record(last, company=9)
    stopper.stop()
    assert any(row.company_id == 9 for row in _rows(UsageDaily)) and not stopper._thread.is_alive()


def test_the_middleware_meters_every_request_without_database_work(client, company_headers):
    usage_meter.clear()
    client.get("/api/users/me", headers=company_headers)  # calienta
    usage_meter.clear()
    with count_queries() as statements:
        response = client.get("/api/users/me", headers=company_headers)
    assert response.status_code == 200 and len(statements) <= 2  # solo la autenticación
    client.get("/api/no-existe")
    client.post("/api/employees", json={}, headers=company_headers)
    batch = usage_meter._batch
    company_id = response.json()["data"]["company"]["id"]
    routes = {key[2]: tally for key, tally in batch.routes.items()}
    me = routes["GET /api/users/me"]
    assert me.requests == 1 and me.bytes_out == len(response.content) and me.duration_ms > 0
    assert routes[OTHER_ROUTE].client_errors == 1  # una URL que no existe no crea su propia fila
    assert routes["POST /api/employees"].bytes_in == 2 and routes["POST /api/employees"].client_errors == 1
    assert any(key[0] == company_id for key in batch.daily)
    usage_meter.clear()


def test_integration_api_usage_counts_for_its_company(client, company_headers):
    from tests.test_api_keys import call, new_key

    secret = new_key(client, company_headers)["secret"]
    usage_meter.clear()
    assert call(client, secret, "/employees").status_code == 200
    company_id = client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]
    assert any(key[0] == company_id and key[2].startswith("GET /api/integrations") for key in usage_meter._batch.routes)
    usage_meter.clear()


def test_a_grain_that_failed_alone_is_retried_alone(monkeypatch):
    """Si solo el total por empresa no se pudo guardar, la siguiente vuelta guarda solo ese grano."""
    meter = UsageMeter(100)
    _record(meter)
    real = meter_module.UsageRepository.add_counts

    def daily_down(self, model, rows):
        if model is UsageDaily:
            raise RuntimeError("tabla bloqueada")
        return real(self, model, rows)

    monkeypatch.setattr(meter_module.UsageRepository, "add_counts", daily_down)
    assert meter.flush() == 2 and meter.pending() == 1
    monkeypatch.setattr(meter_module.UsageRepository, "add_counts", real)
    assert meter.flush() == 1 and len(_rows(UsageRoute)) == 1


def test_noting_the_company_outside_a_request_does_nothing():
    from app.core.request_context import note_company, request_info_var

    assert request_info_var.get() is None
    note_company(5)  # p. ej. una tarea en segundo plano: no hay petición que anotar
    assert request_info_var.get() is None


def test_the_live_channel_counts_messages_and_data(client, company_headers, monkeypatch):
    import json

    usage_meter.clear()
    token = company_headers["Authorization"].removeprefix("Bearer ")
    company_id = client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]
    usage_meter.clear()
    with client.websocket_connect("/api/ws/validation") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        ws.receive_json()
        ws.send_text(json.dumps({"type": "ping"}))
        pong = ws.receive_text()
    channel = {key: tally for key, tally in usage_meter._batch.routes.items() if key[2] == "WS /api/ws/validation"}
    mine = channel[(company_id, next(iter(channel))[1], "WS /api/ws/validation")]
    assert mine.requests == 1 and mine.bytes_in == len('{"type": "ping"}') and mine.bytes_out >= len(pong)
    assert sum(t.requests for t in channel.values()) == 2  # el mensaje de autenticación aún no tiene empresa

    def broken(**_kwargs):
        raise RuntimeError("medidor roto")

    monkeypatch.setattr(usage_meter, "record", broken)
    with client.websocket_connect("/api/ws/validation") as ws:  # medir jamás corta el canal
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        assert ws.receive_json()["code"] == "WS_AUTHENTICATED"
    usage_meter.clear()
