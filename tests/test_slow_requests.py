"""Regla 18 de la raíz con su excepción (decisión del dueño del producto, 2026-10-06): una petición facial alerta al
ADMIN desde `SLOW_REQUEST_FACE_THRESHOLD_MS` (2 500 ms) y cualquier otra desde `SLOW_REQUEST_THRESHOLD_MS` (1 000 ms).

Las rutas faciales viven en UN lugar (`admission.FACE_PREFIXES`): esta prueba falla si una ruta que usa el motor
facial (`Pipeline`) o el reto no está ahí, o si ahí hay una que no lo usa.
"""

import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core.admission import FACE_PREFIXES, is_face_route
from app.core.config import settings
from app.dependencies import get_pipeline
from app.main import API_ROUTERS
from app.middleware import request_id


def _calls(dependant) -> list:
    found = []
    for dependency in dependant.dependencies:
        found.append(dependency.call)
        found.extend(_calls(dependency))
    return found


def _api_routes():
    """(método, ruta concreta con sus parámetros en 1, ¿usa el motor facial o es del reto?) de toda la API."""
    for router in API_ROUTERS:
        for route in router.routes:
            if isinstance(route, APIRoute):
                concrete = "/api" + re.sub(r"\{[^}]+\}", "1", route.path)
                face = get_pipeline in _calls(route.dependant) or route.path.startswith("/face/")
                for method in sorted(route.methods - {"HEAD"}):
                    yield method, concrete, face


def test_the_face_routes_are_exactly_the_ones_that_use_the_face_engine():
    routes = list(_api_routes())
    wrong = [(method, path) for method, path, face in routes if is_face_route(method, path) != face]
    assert wrong == []
    faces = {(method, path) for method, path, face in routes if face}
    # Las que pidió el dueño: registro (propio y en persona), verificación, identificación, reto y asistencia.
    assert {
        ("POST", "/api/enrollment/face"),
        ("POST", "/api/employees/1/face/enroll"),
        ("POST", "/api/employees/1/face/verify"),
        ("POST", "/api/verification/face"),
        ("POST", "/api/checkpoint/identify/face"),
        ("POST", "/api/face/challenge"),
        ("POST", "/api/me/attendance/1"),
    } <= faces
    # Cada prefijo nombra al menos una ruta real (ninguno sobra) y lo demás sigue con el umbral general.
    from app.core.admission import group_of

    groups = {group_of(method, path, depth=None) for method, path in faces}
    assert all(any(group.startswith(prefix) for group in groups) for prefix in FACE_PREFIXES)
    assert not is_face_route("POST", "/api/checkpoint/identify/qr")  # sin rostro
    assert not is_face_route("POST", "/api/employees/1/face/reset")  # pide otro registro: no analiza capturas
    assert not is_face_route("GET", "/api/me/attendance/today")


def _scope(method: str, path: str) -> dict:
    return {"type": "http", "method": method, "path": path}


@pytest.mark.parametrize(
    ("method", "path", "elapsed", "expected"),
    [
        ("POST", "/api/verification/face", 999.0, None),
        ("POST", "/api/verification/face", 2400.0, None),  # facial: hasta 2.5 s no es lenta
        ("POST", "/api/verification/face", 2600.0, 2500),
        ("POST", "/api/me/attendance/check-in", 2600.0, 2500),
        ("POST", "/api/employees/12/face/verify", 3000.0, 2500),
        ("GET", "/api/employees", 1200.0, 1000),  # cualquier otra: desde 1 s
        ("GET", "/api/employees", 1000.0, None),
        ("POST", "/api/checkpoint/identify/qr", 1500.0, 1000),
    ],
)
def test_each_route_class_has_its_threshold(method, path, elapsed, expected):
    assert request_id.slow_threshold_ms(_scope(method, path), elapsed) == expected


def test_a_face_threshold_below_the_general_one_still_applies(monkeypatch):
    """La comparación rápida usa el menor de los dos: ningún ajuste del `.env` deja de alertar lo que debe."""
    monkeypatch.setattr(settings, "SLOW_REQUEST_FACE_THRESHOLD_MS", 500)
    assert request_id.slow_threshold_ms(_scope("POST", "/api/verification/face"), 700.0) == 500
    assert request_id.slow_threshold_ms(_scope("GET", "/api/employees"), 700.0) is None
    assert request_id.slow_threshold_ms({"type": "http"}, 1500.0) == 1000  # sin método ni ruta: el general


def test_the_middleware_alerts_each_route_with_its_own_threshold(monkeypatch):
    """1.8 s en una ruta facial no alerta; en cualquier otra sí (con su umbral en la alerta); 3 s faciales sí."""
    ticks = iter([0.0, 1.8, 10.0, 11.8, 20.0, 23.0])

    class _Clock:
        @staticmethod
        def perf_counter() -> float:
            return next(ticks)

    monkeypatch.setattr(request_id, "time", _Clock)
    app = FastAPI()
    request_id.register_request_id_middleware(app)
    app.add_api_route("/api/verification/face", lambda: {"ok": True}, methods=["POST"])
    app.add_api_route("/api/employees", lambda: {"ok": True})
    request_id.slow_requests.clear()
    with TestClient(app) as test_client:
        assert test_client.post("/api/verification/face").status_code == 200
        assert test_client.get("/api/employees").status_code == 200
        assert request_id.slow_requests.drain().keys() == {"GET /api/employees"}
        assert test_client.post("/api/verification/face").status_code == 200
    face = request_id.slow_requests.drain()["POST /api/verification/face"]
    assert face.threshold_ms == 2500 and face.max_ms == 3000
