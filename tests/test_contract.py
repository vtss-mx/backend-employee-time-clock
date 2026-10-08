"""Contrato único de respuesta: TODAS las respuestas de la API tienen la misma forma."""

import re

import pytest
from fastapi import APIRouter

from app.main import app
from tests.conftest import approved_employee, create_employee
from tests.test_envelope import KEYS as ENVELOPE_KEYS

ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def assert_envelope(response, status: int | None = None) -> dict:
    body = response.json()
    assert set(body) == ENVELOPE_KEYS, body
    if status is not None:
        assert response.status_code == status, response.text
    assert body["statusCode"] == response.status_code
    assert body["success"] is (200 <= response.status_code < 300)
    assert isinstance(body["code"], str) and body["code"]
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["errors"], list)
    if body["success"]:
        assert body["errors"] == []
    else:
        assert body["data"] is None or isinstance(body["data"], dict)
        assert body["errors"] and all({"code", "message"} <= set(e) for e in body["errors"])
    assert body["traceId"] == response.headers["X-Request-ID"]
    assert ISO_UTC.match(body["timestamp"]), body["timestamp"]
    return body


def test_every_endpoint_returns_the_envelope(client, company_headers):
    headers = approved_employee(client, company_headers)
    me = assert_envelope(client.get("/api/users/me", headers=headers), 200)["data"]
    emp_id = me["employee"]["id"]
    other = assert_envelope(create_employee(client, company_headers, number="EMP-9", email="x@empresa.com"), 201)
    assert other["code"] == "EMPLOYEE_CREATED"

    calls = [
        ("get", "/api/health/live", None, 200),
        ("get", "/api/health/ready", None, 200),
        ("get", "/api/health", None, 200),
        ("get", "/api/employees", company_headers, 200),
        ("get", f"/api/employees/{emp_id}", company_headers, 200),
        ("get", f"/api/employees/{emp_id}/qr", company_headers, 200),
        ("get", f"/api/employees/{emp_id}/verifications", company_headers, 200),
        ("get", "/api/enrollments?status=APPROVED", company_headers, 200),
        ("post", "/api/face/challenge", headers, 200),
        ("post", "/api/users/me/qr", headers, 201),
        ("patch", f"/api/employees/{emp_id}/status", company_headers, 200),
        ("delete", f"/api/employees/{other['data']['id']}/qr", company_headers, 200),
        ("delete", f"/api/employees/{other['data']['id']}", company_headers, 200),
    ]
    for method, path, hdrs, status in calls:
        kwargs = {"headers": hdrs or {}}
        if method == "patch":
            kwargs["json"] = {"active": True}
        assert_envelope(getattr(client, method)(path, **kwargs), status)


def test_error_responses_use_the_envelope(client, company_headers):
    cases = [
        (client.get("/api/users/me"), 401, "UNAUTHORIZED"),
        (client.get("/api/no-existe"), 404, "NOT_FOUND"),
        (client.get("/api/employees/999999", headers=company_headers), 404, None),
        (client.put("/api/health/live"), 405, "METHOD_NOT_ALLOWED"),
        (client.post("/api/auth/login", json={"email": "x"}), 422, "VALIDATION_ERROR"),
        (client.post("/api/auth/login", content="{no json", headers={"Content-Type": "application/json"}), 422, None),
        (
            client.post("/api/auth/login", content=b"x", headers={"Content-Length": "999999999"}),
            413,
            "PAYLOAD_TOO_LARGE",
        ),
    ]
    for response, status, code in cases:
        body = assert_envelope(response, status)
        if code:
            assert body["code"] == code
    validation = client.post("/api/auth/login", json={"email": "x"}).json()
    assert {e["field"] for e in validation["errors"]} >= {"email", "password"}


def test_unhandled_exception_keeps_trace_id(client):
    router = APIRouter()

    @router.get("/api/__boom")
    def boom():
        raise RuntimeError("fallo inesperado")

    app.include_router(router)
    try:
        body = assert_envelope(client.get("/api/__boom", headers={"X-Request-ID": "trace-boom-1"}), 500)
        assert body["code"] == "INTERNAL_ERROR" and body["traceId"] == "trace-boom-1"
        assert "fallo inesperado" not in body["message"]  # sin filtrar detalles internos
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/api/__boom"]


@pytest.mark.parametrize("status,expected", [(200, True), (201, True), (204, True), (404, False), (503, False)])
def test_success_is_derived_from_status_code(status, expected):
    from app.core.responses import ApiResponse

    texts = {"message": "Listo", "errors": [], "texts": []}
    response = ApiResponse(statusCode=status, success=not expected, message="Listo", i18n={"es-MX": texts})
    assert response.success is expected
