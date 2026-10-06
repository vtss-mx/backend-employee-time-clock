"""Contrato único: TODA respuesta de la API (éxito o error, de cualquier capa) tiene la misma forma:
success, statusCode, code, message, data, errors[{code, message, field, details}], traceId, timestamp.
El frontend depende de eso para procesar cualquier respuesta y avisar de cualquier falla."""

import pytest
from fastapi.routing import APIRoute

from app.core.config import settings
from app.main import API_ROUTERS

KEYS = {"success", "statusCode", "code", "message", "data", "errors", "traceId", "timestamp"}
ERROR_KEYS = {"code", "message", "field", "details"}


def assert_envelope(response, status: int, code: str | None = None, locale: str = "es-MX") -> dict:
    body = response.json()
    # El sobre dice en qué idioma va y que depende de Accept-Language (regla 16): ningún caché mezcla idiomas.
    assert response.headers["Content-Language"] == locale and "Accept-Language" in response.headers["Vary"]
    assert set(body) == KEYS, f"{response.request.method} {response.request.url}: {sorted(body)}"
    assert body["statusCode"] == response.status_code == status
    assert body["success"] is (status < 400)
    assert isinstance(body["message"], str) and body["message"]
    assert body["traceId"] == response.headers["X-Request-ID"]
    if status >= 400:
        assert body["errors"] and all(set(e) == ERROR_KEYS for e in body["errors"]), body["errors"]
    if code:
        assert body["code"] == code
    return body


def test_every_route_declares_the_envelope():
    """Las respuestas exitosas de cada ruta se documentan y validan como ApiResponse[...]."""
    missing = [
        f"{sorted(route.methods)} {route.path}"
        for router in API_ROUTERS
        for route in router.routes
        if isinstance(route, APIRoute) and not getattr(route.response_model, "__name__", "").startswith("ApiResponse")
    ]
    assert not missing


def test_success_has_the_envelope(client, company_headers):
    assert_envelope(client.get("/api/users/me", headers=company_headers), 200)
    assert_envelope(client.get("/api/health/live"), 200, "ALIVE")
    english = {**company_headers, "Accept-Language": "en-US"}
    assert assert_envelope(client.get("/api/users/me", headers=english), 200, locale="en-US")["message"] == (
        "Authenticated user"
    )


@pytest.mark.parametrize(
    ("method", "url", "kwargs", "status", "code"),
    [
        ("GET", "/api/no-existe", {}, 404, None),  # ruta inexistente
        ("PUT", "/api/catalogs", {}, 405, None),  # método no permitido
        ("GET", "/api/users/me", {}, 401, None),  # sin sesión
        ("GET", "/api/users/me", {"headers": {"Authorization": "Bearer basura"}}, 401, None),  # token inválido
        (
            "POST",
            "/api/auth/login",
            {"content": b"{no-es-json", "headers": {"Content-Type": "application/json"}},
            422,
            None,
        ),
        ("POST", "/api/auth/login", {"json": {"email": "no-es-correo"}}, 422, "VALIDATION_ERROR"),
        ("POST", "/api/auth/login", {"json": {"email": "nadie@empresa.com", "password": "Equivocada1"}}, 401, None),
        ("POST", "/api/face/check", {"headers": {"Content-Length": str(10**9)}}, 413, "PAYLOAD_TOO_LARGE"),
        ("POST", "/api/telemetry/web", {"headers": {"Content-Length": str(10**6)}}, 413, "PAYLOAD_TOO_LARGE"),
        ("POST", "/api/telemetry/web", {"json": {"samples": []}}, 422, "VALIDATION_ERROR"),
        ("GET", "/api/integrations/v1/company", {}, 401, None),  # API de integración sin llave
    ],
)
@pytest.mark.parametrize("locale", ["es-MX", "en-US"])
def test_errors_from_every_layer_have_the_envelope(client, method, url, kwargs, status, code, locale):
    """El mismo contrato en los dos idiomas: solo cambian los textos (`message`, `errors[].message`), nunca los
    códigos."""
    headers = {**kwargs.get("headers", {}), "Accept-Language": locale}
    body = assert_envelope(client.request(method, url, **{**kwargs, "headers": headers}), status, code, locale)
    assert all(error["message"] for error in body["errors"])


def test_forbidden_and_business_errors_have_the_envelope(client, company_headers, admin_headers):
    assert_envelope(client.get("/api/admin/companies", headers=company_headers), 403)  # rol sin permiso
    english = {**company_headers, "Accept-Language": "en-US"}
    forbidden = assert_envelope(client.get("/api/admin/companies", headers=english), 403, locale="en-US")
    assert forbidden["message"] == "You don't have permission for this action"
    assert_envelope(client.get("/api/employees/999999", headers=company_headers), 404, "EMPLOYEE_NOT_FOUND")
    taken = client.post("/api/departments", json={"name": "A"}, headers=company_headers)
    assert_envelope(taken, 201)
    assert_envelope(client.post("/api/departments", json={"name": "a"}, headers=company_headers), 409)
    body = assert_envelope(client.post("/api/departments", json={"name": ""}, headers=company_headers), 422)
    assert body["errors"][0]["field"] == "name"


def test_rate_limit_has_the_envelope(client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1)
    payload = {"email": "admin@empresa.com", "password": "Equivocada1"}
    client.post("/api/auth/login", json=payload)
    response = client.post("/api/auth/login", json=payload, headers={"Accept-Language": "en-US"})
    assert assert_envelope(response, 429, locale="en-US")["message"] == "Too many requests. Try again in a few seconds."
    assert response.headers.get("Retry-After")


def test_unexpected_errors_have_the_envelope_without_leaking_details(client, company_headers, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("detalle interno que el cliente no debe ver")

    from app.routers import catalogs

    monkeypatch.setattr(catalogs, "get_catalogs", boom)
    response = client.get("/api/catalogs", headers=company_headers)
    body = assert_envelope(response, 500, "INTERNAL_ERROR")
    assert "detalle interno" not in response.text and body["data"] is None
