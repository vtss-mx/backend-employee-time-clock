"""Lo que mide el navegador (`POST /api/telemetry/web`): pública a propósito pero acotada (límite por sesión o IP
antes de leer, 64 KB, campos estrictos, tope de muestras); sin sesión solo cuenta la pantalla de inicio de sesión;
todo se normaliza a plantillas y suma en memoria, sin escribir en la BD."""

import pytest

from app.core.config import settings
from app.core.perf_meter import OTHER, perf_meter
from app.schemas.performance import MAX_WEB_BODY_BYTES
from app.services import web_performance
from tests.test_performance import count_queries

URL = "/api/telemetry/web"


def _sample(kind: str, name: str, value: float = 100, status: int | None = None) -> dict:
    sample: dict = {"kind": kind, "name": name, "value": value}
    if status is not None:
        sample["status"] = status
    return sample


def _recorded() -> dict[tuple[str, str], object]:
    return {(kind, name): stat for (_, kind, name), stat in perf_meter.drain().items() if kind != "HTTP"}


def test_a_session_records_vitals_long_tasks_and_api_times_as_templates(client, company_headers):
    perf_meter.clear()
    samples = [
        _sample("LCP", "/company/employees/12?tab=1#x", 1800),
        _sample("INP", "/company/employees/{id}", 90),
        _sample("CLS", "/company/employees/7", 0.12),
        _sample("FCP", "/company/dashboard", 700),
        _sample("TTFB", "/company/dashboard", 150),
        _sample("LONG_TASK", "/company/dashboard", 120),
        _sample("API", "GET /api/employees/{id}", 80, 200),
        _sample("API", "GET /api/employees/5?page=2", 90, 404),
        _sample("API", "POST /api/employees", 300, 503),
        _sample("API", "GET /api/users/me", 20, 0),  # sin red
        _sample("API", "GET /api/users/me", 15000, 408),  # tiempo agotado del cliente
        _sample("API", "GET /api/no-existe", 10),  # ninguna ruta real: OTHER (y sin código: ni falla ni rechazo)
        _sample("API", "FETCH /api/x", 10),  # no es una petición: se descarta
        _sample("LCP", "sin-diagonal", 10),  # no es una pantalla: se descarta
    ]
    response = client.post(URL, json={"app_version": "abc123", "samples": samples}, headers=company_headers)
    assert response.status_code == 202 and response.json()["code"] == "WEB_PERFORMANCE_RECORDED"
    assert response.json()["data"] == {"accepted": 12, "dropped": 2}
    stats = _recorded()
    assert stats[("WEB_LCP", "/company/employees/{id}")].count == 1
    assert stats[("WEB_INP", "/company/employees/{id}")].count == 1
    assert stats[("WEB_CLS", "/company/employees/{id}")].max_ms == 120  # el puntaje × 1000
    employees = stats[("WEB_API", "GET /api/employees/{employee_id}")]
    assert (employees.count, employees.errors, employees.client_errors) == (2, 0, 1)
    assert stats[("WEB_API", "POST /api/employees")].errors == 1
    me = stats[("WEB_API", "GET /api/users/me")]
    assert (me.count, me.errors, me.client_errors) == (2, 2, 0)
    other = stats[("WEB_API", OTHER)]
    assert (other.count, other.errors, other.client_errors) == (1, 0, 0)
    assert stats[("WEB_LONG_TASK", "/company/dashboard")].total_ms == 120


def test_without_a_session_only_the_login_screen_counts(client):
    perf_meter.clear()
    samples = [
        _sample("LCP", "/login", 1200),
        _sample("API", "POST /api/auth/login", 250, 401),
        _sample("LCP", "/admin/dashboard", 900),  # sin sesión: no
        _sample("API", "GET /api/admin/stats", 90, 200),  # sin sesión: no
    ]
    for headers in ({}, {"Authorization": "Bearer vencido"}, {"Authorization": "Basic abc"}):
        response = client.post(URL, json={"samples": samples}, headers=headers)
        assert response.status_code == 202, response.text  # un token inválido nunca es un 401: es anónimo
        assert response.json()["data"] == {"accepted": 2, "dropped": 2}
    stats = _recorded()
    assert stats[("WEB_LCP", "/login")].count == 3 and stats[("WEB_API", "POST /api/auth/login")].client_errors == 3
    assert ("WEB_LCP", "/admin/dashboard") not in stats


def test_a_batch_never_writes_to_the_database(client):
    """El lote solo suma en memoria (el límite por IP, con el respaldo de memoria de las pruebas, tampoco consulta)."""
    with count_queries() as statements:
        response = client.post(URL, json={"samples": [_sample("FCP", "/login", 300)]})
    assert response.status_code == 202 and statements == []


def test_sessions_and_anonymous_senders_have_their_own_limits(client, company_headers, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_WEB_PERF_ANONYMOUS_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_WEB_PERF_PER_MINUTE", 2)
    body = {"samples": [_sample("FCP", "/login", 300)]}
    assert client.post(URL, json=body).status_code == 202
    limited = client.post(URL, json=body)
    assert limited.status_code == 429 and limited.headers["Retry-After"]
    for _ in range(2):  # una sesión no comparte el límite de su IP
        assert client.post(URL, json=body, headers=company_headers).status_code == 202
    assert client.post(URL, json=body, headers=company_headers).status_code == 429


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"samples": []}, "samples"),
        ({"samples": [{"kind": "FPS", "name": "/login", "value": 1}]}, "samples.0.kind"),
        ({"samples": [{"kind": "LCP", "name": "/login", "value": -1}]}, "samples.0.value"),
        ({"samples": [{"kind": "LCP", "name": "/login", "value": 1, "user": "x@y.com"}]}, "samples.0.user"),
        ({"samples": [{"kind": "API", "name": "GET /api/x", "value": 1, "status": 700}]}, "samples.0.status"),
        ({"samples": [_sample("LCP", "/login")], "email": "x@y.com"}, "email"),
    ],
)
def test_fields_are_strict(client, body, field):
    response = client.post(URL, json=body)
    assert response.status_code == 422 and response.json()["errors"][0]["field"] == field


def test_batches_are_bounded_in_samples_and_size(client, monkeypatch):
    monkeypatch.setattr(settings, "PERF_WEB_MAX_SAMPLES", 2)
    too_many = client.post(URL, json={"samples": [_sample("FCP", "/login")] * 3})
    assert too_many.status_code == 422 and too_many.json()["code"] == "TOO_MANY_SAMPLES"
    assert too_many.json()["errors"][0]["field"] == "samples"
    declared = client.post(URL, content=b"{}", headers={"Content-Length": str(MAX_WEB_BODY_BYTES + 1)})
    assert declared.status_code == 413
    huge = client.post(URL, json={"samples": [_sample("FCP", "/" + "x" * 190)] * 400})
    assert huge.status_code == 413 and huge.json()["code"] == "PAYLOAD_TOO_LARGE"


def test_route_templates_resolve_named_parameters_and_ignore_other_routes():
    from fastapi import APIRouter

    router = APIRouter()
    router.add_api_route("/items/{item_id}", lambda item_id: None, methods=["GET"])
    router.add_api_websocket_route("/ws", lambda websocket: None)  # el canal en vivo no es una ruta HTTP
    index = web_performance.route_index([router], "/api")
    assert index.resolve("GET", "/api/items/12") == "GET /api/items/{item_id}"
    assert index.resolve("GET", "/api/ws") == OTHER
