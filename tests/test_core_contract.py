"""Contrato de respuesta y contexto de la petición en los casos límite (app/core: exceptions,
responses, request_context; app/middleware/request_id.py)."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.exceptions import register_exception_handlers
from app.core.request_context import RequestInfo, note_actor, request_info_var
from app.core.responses import ApiResponse
from app.middleware import request_id
from app.services.department_service import DepartmentService


def test_unique_rule_collision_between_simultaneous_requests_answers_409(client, company_headers, monkeypatch):
    """Dos altas simultáneas con el mismo nombre pasan la revisión previa; la segunda choca con el
    índice único al guardar: 409 CONCURRENT_UPDATE reintentable (manejador global), nunca un 500."""
    monkeypatch.setattr(DepartmentService, "_ensure_name_free", lambda *_args, **_kwargs: None)
    body = {"name": "Producción", "description": None}
    assert client.post("/api/departments", json=body, headers=company_headers).status_code == 201
    second = client.post("/api/departments", json=body, headers=company_headers)
    assert second.status_code == 409
    assert second.json()["code"] == "CONCURRENT_UPDATE" and second.json()["success"] is False
    listed = client.get("/api/departments", headers=company_headers).json()["data"]
    assert listed["total"] == 1  # la que chocó no dejó nada a medias


def _streaming_app() -> FastAPI:
    """La misma pila de errores que la API (traceId + manejadores) con una respuesta por partes que
    falla a la mitad: el estado 200 ya salió y no puede cambiarse por un 500."""
    app = FastAPI()
    request_id.register_request_id_middleware(app)
    register_exception_handlers(app)

    def chunks():
        yield b'{"parcial": '
        raise RuntimeError("se cayó a la mitad")

    @app.get("/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(chunks(), media_type="application/json")

    return app


def test_failure_after_the_response_started_is_logged_and_reported(monkeypatch, caplog):
    events: list = []
    monkeypatch.setattr(request_id.error_reporter, "report", events.append)
    with TestClient(_streaming_app(), raise_server_exceptions=False) as test_client:
        response = test_client.get("/stream")
    assert response.status_code == 200 and response.headers["X-Request-ID"]
    assert "Error no controlado: se cayó a la mitad" in caplog.text  # manejador global de último recurso
    (event,) = events
    assert (event.http_status, event.code, event.exception_type) == (500, "INTERNAL_ERROR", "RuntimeError")
    assert event.trace_id == response.headers["X-Request-ID"] and "se cayó a la mitad" in event.detail


def test_slow_requests_are_logged_with_their_trace_id(monkeypatch, caplog):
    """Una petición más lenta que SLOW_REQUEST_THRESHOLD_MS (regla 18) deja aviso en el log (con su traceId) y suma a
    la alerta de su ruta (en memoria, se guarda en lotes)."""
    ticks = iter([100.0, 104.5])

    class _Clock:
        @staticmethod
        def perf_counter() -> float:
            return next(ticks)

    monkeypatch.setattr(request_id, "time", _Clock)
    app = FastAPI()
    request_id.register_request_id_middleware(app)
    app.add_api_route("/lenta", lambda: {"ok": True})
    with caplog.at_level(logging.WARNING, logger="app.access"), TestClient(app) as test_client:
        response = test_client.get("/lenta")
    assert response.status_code == 200
    assert f"Petición lenta GET /lenta 4500 ms [{response.headers['X-Request-ID']}]" in caplog.text
    assert request_id.slow_requests.drain()["GET /lenta"].last_trace_id == response.headers["X-Request-ID"]


def test_a_body_that_is_not_an_object_is_invalid_not_a_crash():
    """`success` se deriva de statusCode solo si hay un objeto; otro JSON (lista, texto) se rechaza
    como inválido con ValidationError, sin romper el validador."""
    with pytest.raises(ValidationError):
        ApiResponse[int].model_validate([200, "OK"])
    assert ApiResponse[int].model_validate({"statusCode": 404, "success": True, "message": "x"}).success is False


def test_noting_the_actor_outside_a_request_is_harmless():
    """Hilos en segundo plano (mantenimiento, registro de errores) usan los mismos servicios: fuera de
    una petición no hay a quién anotarle el usuario y no pasa nada."""
    assert request_info_var.get() is None
    note_actor(7, 3)
    info = RequestInfo()
    token = request_info_var.set(info)
    try:
        note_actor(7, 3)
    finally:
        request_info_var.reset(token)
    assert (info.user_id, info.company_id) == (7, 3)
