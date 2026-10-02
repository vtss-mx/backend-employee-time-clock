"""Canal WebSocket de validación en tiempo real (número de empleado y correo)."""

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from tests.conftest import COMPANY_EMAIL, COMPANY_PASSWORD, create_employee, login

URL = "/api/ws/validation"
ENVELOPE_KEYS = {"success", "statusCode", "code", "message", "data", "errors", "traceId", "timestamp"}


def token(client, email=COMPANY_EMAIL, password=COMPANY_PASSWORD) -> str:
    return login(client, email, password)["Authorization"].removeprefix("Bearer ")


def connect(client, access_token):
    ws = client.websocket_connect(URL)
    socket = ws.__enter__()
    socket.send_json({"type": "auth", "token": access_token})
    ready = socket.receive_json()
    assert ready["code"] == "WS_AUTHENTICATED", ready
    return ws, socket


def validate(socket, value, field="employee_number", exclude=None, msg_id="req-00000001"):
    socket.send_json({"type": "validate", "id": msg_id, "field": field, "value": value, "excludeId": exclude})
    return socket.receive_json()


def test_realtime_employee_number_and_email(client, company_headers):
    emp = create_employee(client, company_headers).json()["data"]  # EMP-001 / juan@empresa.com
    ws, socket = connect(client, token(client))
    try:
        taken = validate(socket, " emp-001 ", msg_id="req-taken-1")
        assert set(taken) == ENVELOPE_KEYS  # contrato único también en WebSocket
        assert taken["traceId"] == "req-taken-1"  # correlación pregunta/respuesta
        assert taken["code"] == "TAKEN" and taken["data"]["normalized"] == "EMP-001" and not taken["data"]["available"]

        free = validate(socket, "EMP-777")
        assert free["code"] == "AVAILABLE" and free["success"] and free["data"]["available"]

        editing = validate(socket, "EMP-001", exclude=emp["id"])  # al editar, su propio número es válido
        assert editing["code"] == "AVAILABLE"

        invalid = validate(socket, "con espacios!")
        assert invalid["code"] == "INVALID_FORMAT" and invalid["statusCode"] == 422 and not invalid["success"]
        assert validate(socket, "   ")["code"] == "EMPTY"

        assert validate(socket, "JUAN@empresa.com", field="email")["code"] == "TAKEN"
        assert validate(socket, "otro@empresa.com", field="email")["code"] == "AVAILABLE"
        assert validate(socket, "juan@empresa.com", field="email", exclude=emp["id"])["code"] == "AVAILABLE"
        assert validate(socket, "no-es-correo", field="email")["code"] == "INVALID_FORMAT"

        socket.send_json({"type": "ping", "id": "ping-0001"})
        assert socket.receive_json()["code"] == "PONG"
        socket.send_text("{no json")
        assert socket.receive_json()["code"] == "BAD_MESSAGE"
        socket.send_json({"type": "validate", "field": "password", "value": "x"})
        assert socket.receive_json()["code"] == "BAD_MESSAGE"
    finally:
        ws.__exit__(None, None, None)


def test_realtime_requires_company_auth(client, company_headers):
    with client.websocket_connect(URL) as socket:
        socket.send_json({"type": "validate", "field": "email", "value": "x"})
        assert socket.receive_json()["code"] == "UNAUTHORIZED"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401

    with client.websocket_connect(URL) as socket:
        socket.send_json({"type": "auth", "token": "basura"})
        assert socket.receive_json()["code"] == "TOKEN_INVALID"

    create_employee(client, company_headers)
    employee_token = token(client, "juan@empresa.com", "Empleado123")
    with client.websocket_connect(URL) as socket:
        socket.send_json({"type": "auth", "token": employee_token})
        assert socket.receive_json()["statusCode"] == 403
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4403


def test_realtime_auth_timeout_and_limits(client, monkeypatch):
    monkeypatch.setattr(settings, "WS_AUTH_TIMEOUT_SECONDS", 0.05)
    with client.websocket_connect(URL) as socket:
        assert socket.receive_json()["code"] == "WS_AUTH_TIMEOUT"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4408

    monkeypatch.setattr(settings, "WS_AUTH_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(settings, "WS_MAX_MESSAGES_PER_10S", 2)
    ws, socket = connect(client, token(client))
    try:
        validate(socket, "EMP-1")
        validate(socket, "EMP-2")
        assert validate(socket, "EMP-3")["code"] == "RATE_LIMITED"
        socket.send_text("x" * (settings.WS_MAX_MESSAGE_BYTES + 1))
        assert socket.receive_json()["code"] in {"BAD_MESSAGE", "RATE_LIMITED"}
    finally:
        ws.__exit__(None, None, None)

    monkeypatch.setattr(settings, "WS_MAX_CONNECTIONS", 0)
    with client.websocket_connect(URL) as socket:
        assert socket.receive_json()["code"] == "SERVER_BUSY"


def test_logout_closes_the_realtime_channel(client):
    access = token(client)
    ws, socket = connect(client, access)
    try:
        client.post("/api/auth/logout", headers={"Authorization": f"Bearer {access}"})
        assert validate(socket, "EMP-9")["code"] == "SESSION_REVOKED"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401
    finally:
        ws.__exit__(None, None, None)


def test_http_fallback_availability(client, company_headers):
    emp = create_employee(client, company_headers).json()["data"]
    url = "/api/employees/availability"
    taken = client.get(url, params={"field": "employee_number", "value": "emp-001"}, headers=company_headers)
    assert taken.status_code == 200 and taken.json()["code"] == "TAKEN"
    mine = client.get(
        url, params={"field": "employee_number", "value": "EMP-001", "exclude_id": emp["id"]}, headers=company_headers
    )
    assert mine.json()["data"]["available"] is True
    bad = client.get(url, params={"field": "password", "value": "x"}, headers=company_headers)
    assert bad.status_code == 422
