"""Rutas en sus casos límite: cookies, renovación sin rotar, tablero de aprendizaje facial, rechazo de
un registro y el canal de validación en tiempo real ante fallas."""

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from starlette.websockets import WebSocketDisconnect

from app.core import opaque_tokens
from app.core.config import settings
from app.models import Screen
from app.routers import realtime
from tests.conftest import COMPANY_EMAIL, COMPANY_PASSWORD, approved_employee
from tests.test_realtime import URL, connect, token, validate
from tests.test_screens import _revoke

COOKIE = settings.REFRESH_COOKIE_NAME
CREDENTIALS = {"email": COMPANY_EMAIL, "password": COMPANY_PASSWORD}


# ---------------------------------------------------------------- autenticación


@pytest.mark.parametrize(
    ("mode", "url", "secure"),
    [
        ("true", "http://testserver/api/auth/login", True),  # detrás de un proxy que termina HTTPS
        ("false", "https://testserver/api/auth/login", False),  # forzado aunque llegue por HTTPS
    ],
)
def test_cookie_secure_can_be_forced_regardless_of_the_scheme(client, monkeypatch, mode, url, secure):
    monkeypatch.setattr(settings, "COOKIE_SECURE", mode)
    response = client.post(url, json=CREDENTIALS)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=") and ("; secure" in cookie.lower()) is secure


def test_retry_after_rotating_the_server_key_renews_without_touching_the_cookie(client, monkeypatch):
    """La respuesta de una renovación se perdió y, entretanto, cambió la llave de rotación (nueva
    DATA_ENCRYPTION_KEY al reiniciar). El reintento con la cookie anterior, dentro de la gracia,
    renueva el acceso pero no entrega otra cookie: el siguiente secreto ya no se puede recalcular."""
    assert client.post("/api/auth/login", json=CREDENTIALS).status_code == 200
    first = client.cookies.get(COOKIE)
    assert client.post("/api/auth/refresh").status_code == 200
    monkeypatch.setattr(opaque_tokens, "_ROTATION_KEY", b"otra-llave-tras-reiniciar")
    client.cookies.set(COOKIE, first, path="/api/auth")
    retry = client.post("/api/auth/refresh")
    assert retry.status_code == 200 and retry.json()["code"] == "TOKEN_REFRESHED"
    assert retry.json()["data"]["access_token"] and "set-cookie" not in retry.headers


# ---------------------------------------------------------------- empresa


def test_company_sees_how_face_recognition_evolves(client, company_headers):
    approved_employee(client, company_headers)
    response = client.get("/api/employees/face/learning", headers=company_headers)
    assert response.status_code == 200 and response.json()["code"] == "FACE_LEARNING_SUMMARY"
    summary = response.json()["data"]
    assert summary["approved_employees"] == 1
    assert (summary["employees_learning"], summary["learned_samples"], summary["last_learned_at"]) == (0, 0, None)


def test_platform_admin_has_no_company_policy_to_read(client, admin_headers):
    """La política es de una empresa; el ADMIN de la plataforma no opera ninguna."""
    response = client.get("/api/settings/verification", headers=admin_headers)
    assert response.status_code == 403 and response.json()["code"] == "COMPANY_REQUIRED"


def test_rejection_reason_made_only_of_spaces_is_not_a_reason(client, company_headers):
    response = client.post("/api/enrollments/1/reject", json={"reason": "  a     "}, headers=company_headers)
    assert response.status_code == 422
    assert response.json()["errors"][0]["message"] == "Indica el motivo del rechazo"


# ---------------------------------------------------------------- canal de validación


def _closed_with(socket) -> int:
    with pytest.raises(WebSocketDisconnect) as closed:
        socket.receive_json()
    return closed.value.code


def test_channel_answers_503_and_asks_to_retry_when_the_database_is_down(client, monkeypatch):
    access = token(client)

    def database_down(*_args):
        raise OperationalError("SELECT", {}, Exception("bd caída"))

    monkeypatch.setattr(realtime, "_authenticate", database_down)
    with client.websocket_connect(URL) as socket:
        socket.send_json({"type": "auth", "token": access})
        answer = socket.receive_json()
        assert (answer["statusCode"], answer["code"]) == (503, "DATABASE_UNAVAILABLE")
        assert _closed_with(socket) == 1013  # "inténtalo más tarde"


def test_channel_closes_when_the_session_expires_while_open(client, monkeypatch):
    ws, socket = connect(client, token(client))
    try:
        later = SimpleNamespace(time=lambda: 10**12, monotonic=realtime.time.monotonic)
        monkeypatch.setattr(realtime, "time", later)  # pasaron las 12 h de la sesión
        assert validate(socket, "EMP-1", msg_id="req-expired")["code"] == "TOKEN_EXPIRED"
        assert _closed_with(socket) == 4401
    finally:
        ws.__exit__(None, None, None)


def test_screen_removed_from_the_role_while_the_channel_is_open(client):
    """Se le retira la pantalla al rol con el canal abierto: esa validación responde 403 y el canal
    sigue abierto para lo demás."""
    ws, socket = connect(client, token(client))
    try:
        _revoke("COMPANY", Screen.COMPANY_EMPLOYEES)
        denied = validate(socket, "EMP-1")
        assert (denied["statusCode"], denied["code"]) == (403, "FIELD_NOT_ALLOWED")
        assert validate(socket, "recepcion@empresa.com", field="validator_email")["code"] == "AVAILABLE"
    finally:
        ws.__exit__(None, None, None)


def test_unexpected_channel_failure_closes_with_1011_and_is_logged(client, monkeypatch, caplog):
    ws, socket = connect(client, token(client))

    def broken(*_args):
        raise RuntimeError("falla inesperada")

    try:
        monkeypatch.setattr(realtime, "_validate", broken)
        socket.send_json({"type": "validate", "field": "employee_number", "value": "EMP-1"})
        assert _closed_with(socket) == 1011
    finally:
        ws.__exit__(None, None, None)
    assert "Error en el canal de validación" in caplog.text


def test_channel_rate_limit_frees_up_as_the_window_moves(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(realtime, "time", SimpleNamespace(monotonic=lambda: now[0]))
    limiter = realtime._RateLimiter(limit=2, window=10)
    assert limiter.allow() and limiter.allow() and not limiter.allow()
    now[0] = 10.0  # los dos primeros salen de la ventana
    assert limiter.allow() and limiter.allow() and not limiter.allow()
