"""Fallas de la aplicación web (`POST /api/client-errors`): lo que el navegador reporta llega a
"Errores del sistema" (origen CLIENT) con la pantalla, la versión, el navegador y, si hay sesión,
quién. Es público (una pantalla puede romperse en el login), así que va acotado: límite por IP, tope
de tamaño, campos estrictos y nunca un secreto guardado."""

import logging

import pytest
from sqlalchemy.exc import OperationalError

from app.core.admission import Tier, tier_of
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import ErrorReport
from app.schemas.client_error import MAX_BODY_BYTES
from app.services.client_error_service import exception_type_of
from app.services.error_reporter import error_reporter
from app.services.session_service import SessionService
from tests.conftest import IPHONE_UA
from tests.test_envelope import assert_envelope

URL = "/api/client-errors"
CRASH = {
    "kind": "CRASH",
    "message": "TypeError: Cannot read properties of undefined (reading 'name')",
    "stack": "TypeError: Cannot read properties of undefined\n    at EmployeeForm (index-abc.js:1:2)",
    "path": "/company/employees/12/edit",
    "component": "EmployeeForm",
    "detail": "at EmployeeForm\nat Panel",
    "app_version": "2026.10.04-1",
}


def _saved() -> list[ErrorReport]:
    """Lo que se guardó de la app web (origen CLIENT)."""
    error_reporter.flush()
    with SessionLocal() as db:
        return list(db.query(ErrorReport).filter_by(source="CLIENT"))


def _occurrence(client, admin_headers, code: str) -> tuple[dict, dict]:
    error_reporter.flush()
    reports = client.get("/api/admin/errors", params={"search": code}, headers=admin_headers).json()["data"]["items"]
    (report,) = [r for r in reports if r["source"] == "CLIENT"]
    url = f"/api/admin/errors/{report['id']}/occurrences"
    return report, client.get(url, headers=admin_headers).json()["data"]["items"][0]


def test_a_crash_without_a_session_is_recorded_with_its_screen(client, admin_headers):
    response = client.post(URL, json=CRASH)
    body = assert_envelope(response, 202, "CLIENT_ERROR_RECORDED")
    assert body["data"] is None
    report, occurrence = _occurrence(client, admin_headers, "CLIENT_CRASH")
    assert (report["source"], report["severity"], report["status"]) == ("CLIENT", "CRITICAL", "PENDING")
    assert report["location"] == "/company/employees/{id}/edit"  # la pantalla, sin el id de ese registro
    assert (report["exception_type"], report["http_status"], report["method"]) == ("TypeError", None, None)
    assert report["last_trace_id"] == response.headers["X-Request-ID"]
    detail = client.get(f"/api/admin/errors/{report['id']}", headers=admin_headers).json()["data"]
    assert detail["detail"] == CRASH["stack"]
    assert occurrence["user_label"] is None and occurrence["company_name"] is None
    assert occurrence["context"] == {
        "client": {
            "kind": "CRASH",
            "path": "/company/employees/12/edit",
            "component": "EmployeeForm",
            "detail": "at EmployeeForm\nat Panel",
            "app_version": "2026.10.04-1",
            "user_agent": IPHONE_UA,
            "ip": "testclient",
        },
        "user": None,
        "company_id": None,
    }


def test_the_same_failure_on_another_record_is_the_same_report(client):
    for employee_id in (12, 34):
        assert client.post(URL, json={**CRASH, "path": f"/company/employees/{employee_id}/edit"}).status_code == 202
    (report,) = _saved()
    assert report.occurrences == 2


def test_with_a_session_it_says_who_and_from_which_company(client, admin_headers, company_headers):
    assert client.post(URL, json=CRASH, headers=company_headers).status_code == 202
    _report, occurrence = _occurrence(client, admin_headers, "CLIENT_CRASH")
    assert occurrence["user_label"] == "admin@empresa.com (Company)" and occurrence["company_name"]
    context = occurrence["context"]
    assert context["user"] == {"id": context["user"]["id"], "email": "admin@empresa.com", "role": "COMPANY"}
    assert context["company_id"]


@pytest.mark.parametrize("authorization", ["Bearer basura", "Bearer ", "Basic YWRtaW46c2VjcmV0"])
def test_an_invalid_session_never_loses_the_report(client, authorization, caplog):
    with caplog.at_level(logging.INFO, logger="app.services.client_error_service"):
        response = client.post(URL, json=CRASH, headers={"Authorization": authorization})
    assert response.status_code == 202
    (report,) = _saved()
    assert report.code == "CLIENT_CRASH"
    if authorization == "Bearer basura":
        assert "sesión no válida (TOKEN_INVALID)" in caplog.text


def test_without_the_database_to_know_who_it_still_records(client, company_headers, monkeypatch, caplog):
    def database_down(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("bd caída"))

    monkeypatch.setattr(SessionService, "authenticate_access", database_down)
    assert client.post(URL, json=CRASH, headers=company_headers).status_code == 202
    (report,) = _saved()
    assert report.code == "CLIENT_CRASH"
    assert "Sin BD para saber quién reportó" in caplog.text


def test_a_platform_configuration_failure_is_an_error_not_critical(client):
    config = {
        "kind": "CONFIG",
        "message": "MapsApiError: places: denied (REQUEST_DENIED)",
        "path": "/company/sites/new",
        "component": "GoogleMaps:places",
    }
    unhandled = {"kind": "UNHANDLED", "message": "Algo salió mal", "path": "/login"}
    for payload in (config, unhandled):
        assert client.post(URL, json=payload).status_code == 202
    saved = {r.code: r for r in _saved()}
    assert (saved["CLIENT_CONFIG"].severity, saved["CLIENT_CONFIG"].exception_type) == ("ERROR", "MapsApiError")
    assert saved["CLIENT_CONFIG"].location == "/company/sites/new"
    no_type = saved["CLIENT_UNHANDLED"]
    assert (no_type.severity, no_type.exception_type, no_type.detail) == ("CRITICAL", None, None)


def test_secrets_in_what_the_browser_sends_are_never_stored(client, admin_headers):
    jwt = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiIxIn0.firma-del-token"
    payload = {
        **CRASH,
        "message": f"Error: falló con password=MiClave123 y Bearer {jwt}",
        "stack": f'Error: {{"token": "abc123", "nombre": "Ana"}}\n    at login ({jwt})',
        "component": "Login?api_key=llave-secreta&x=1",
        "detail": "Authorization: Bearer otro.token.secreto",
    }
    assert client.post(URL, json=payload).status_code == 202
    report, occurrence = _occurrence(client, admin_headers, "CLIENT_CRASH")
    detail = client.get(f"/api/admin/errors/{report['id']}", headers=admin_headers).json()["data"]["detail"]
    assert report["message"] == "Error: falló con password=[oculto] y Bearer [oculto]"
    assert detail == 'Error: {"token": [oculto], "nombre": "Ana"}\n    at login ([oculto])'
    assert occurrence["context"]["client"]["component"] == "Login?api_key=[oculto]&x=1"
    assert occurrence["context"]["client"]["detail"] == "Authorization: [oculto]"


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({**CRASH, "kind": "OTRO"}, "kind"),
        ({**CRASH, "path": "/company/employees?search=ana"}, "path"),  # sin query: puede llevar datos
        ({**CRASH, "path": "company"}, "path"),
        ({**CRASH, "message": ""}, "message"),
        ({**CRASH, "message": "x" * 1001}, "message"),
        ({**CRASH, "stack": "x" * 8001}, "stack"),
        ({**CRASH, "component": "x" * 501}, "component"),
        ({**CRASH, "user": "ana@correo.com"}, "user"),  # solo los campos del contrato
    ],
)
def test_fields_are_strictly_validated(client, payload, field):
    body = assert_envelope(client.post(URL, json=payload), 422, "VALIDATION_ERROR")
    assert body["errors"][0]["field"] == field
    assert _saved() == []  # un reporte inválido no se registra (es un 4xx)


@pytest.mark.parametrize("content", [b"{no-es-json", b""])
def test_a_body_that_is_not_json_is_a_validation_error(client, content):
    response = client.post(URL, content=content, headers={"Content-Type": "application/json"})
    assert_envelope(response, 422, "VALIDATION_ERROR")


def test_the_body_has_its_own_small_limit(client):
    huge = b'{"kind": "CRASH", "message": "' + b"x" * MAX_BODY_BYTES + b'"}'
    declared = client.post(URL, content=huge, headers={"Content-Type": "application/json"})
    body = assert_envelope(declared, 413, "PAYLOAD_TOO_LARGE")
    assert "16 KB" in body["message"]

    def chunks():  # sin Content-Length: se cuenta mientras llega y se corta al pasarse
        for start in range(0, len(huge), 4096):
            yield huge[start : start + 4096]

    streamed = client.post(URL, content=chunks(), headers={"Content-Type": "application/json"})
    assert_envelope(streamed, 413, "PAYLOAD_TOO_LARGE")
    assert _saved() == []


def test_reports_are_rate_limited_per_ip(client, monkeypatch, caplog):
    monkeypatch.setattr(settings, "RATE_LIMIT_CLIENT_ERRORS_PER_MINUTE", 1)
    assert client.post(URL, json=CRASH).status_code == 202
    with caplog.at_level(logging.INFO, logger="app.access"):
        limited = client.post(URL, json=CRASH)
    assert_envelope(limited, 429, "RATE_LIMITED")
    assert limited.headers["Retry-After"]
    assert "Respuesta 429 RATE_LIMITED en POST /api/client-errors" in caplog.text
    (report,) = _saved()  # solo el primero: el 429 no es una falla
    assert report.occurrences == 1


def test_reports_wait_their_turn_when_the_api_is_saturated():
    """Con la API saturada se descartan primero: la app no los reintenta."""
    assert tier_of("POST client-errors") == Tier.BACKGROUND


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("TypeError: x is not a function", "TypeError"),
        ("DOMException: The operation was aborted", "DOMException"),
        ("Error: algo", "Error"),
        ("Algo salió mal", None),
        ("TypeError sin dos puntos", None),
    ],
)
def test_the_exception_type_comes_from_the_message_prefix(message, expected):
    assert exception_type_of(message) == expected
