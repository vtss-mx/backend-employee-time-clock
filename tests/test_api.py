from app.core.crypto import decrypt_bytes
from app.core.database import SessionLocal
from app.models import EmployeeQr
from app.services.qr_service import QR_PREFIX
from tests.conftest import approved_employee, create_employee, login


def _qr_content(employee_id: int) -> str:
    with SessionLocal() as db:
        qr = db.query(EmployeeQr).filter_by(employee_id=employee_id, active=True).one()
        return QR_PREFIX + decrypt_bytes(qr.token_encrypted).decode()


# ---------------- Autenticación y roles ----------------


def test_login_and_me(client, company_headers):
    response = client.get("/api/users/me", headers=company_headers)
    assert response.status_code == 200
    assert response.json()["data"]["role"] == "COMPANY"


def test_login_wrong_password(client):
    response = client.post("/api/auth/login", json={"email": "admin@empresa.com", "password": "nope"})
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"


def test_protected_requires_token(client):
    assert client.get("/api/employees").status_code == 401


def test_employee_cannot_access_admin(client, company_headers):
    create_employee(client, company_headers)
    employee_headers = login(client, "juan@empresa.com", "Empleado123")
    assert client.get("/api/employees", headers=employee_headers).status_code == 403
    assert create_employee(client, employee_headers, number="X1", email="x@x.com").status_code == 403


def test_company_cannot_use_verification(client, company_headers):
    response = client.post("/api/verification/qr", json={"qr_content": "x"}, headers=company_headers)
    assert response.status_code == 403


# ---------------- Empleados ----------------


def test_create_employee_with_qr(client, company_headers):
    response = create_employee(client, company_headers)
    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["employee_number"] == "EMP-001"
    assert body["has_face"] is False and body["has_active_qr"] is True
    assert "password" not in body and "password_hash" not in body


def test_create_employee_validations(client, company_headers):
    create_employee(client, company_headers)
    assert create_employee(client, company_headers, email="otro@empresa.com").status_code == 409
    assert create_employee(client, company_headers, number="EMP-002").status_code == 409

    bad = client.post(
        "/api/employees",
        json={
            "first_name": "A",
            "last_name": "B",
            "birth_date": "2999-01-01",
            "employee_number": "E2",
            "email": "no-es-correo",
            "password": "debil",
        },
        headers=company_headers,
    )
    assert bad.status_code == 422
    fields = {e["field"] for e in bad.json()["errors"]}
    assert {"email", "password", "birth_date"} <= fields


def test_update_and_status(client, company_headers):
    emp = create_employee(client, company_headers).json()["data"]
    response = client.put(f"/api/employees/{emp['id']}", json={"first_name": "Juan Carlos"}, headers=company_headers)
    assert response.status_code == 200
    assert response.json()["data"]["first_name"] == "Juan Carlos"

    employee_headers = login(client, "juan@empresa.com", "Empleado123")
    client.patch(f"/api/employees/{emp['id']}/status", json={"active": False}, headers=company_headers)
    # Token vigente deja de funcionar y no puede volver a iniciar sesión.
    assert client.get("/api/users/me", headers=employee_headers).status_code == 401
    response = client.post("/api/auth/login", json={"email": "juan@empresa.com", "password": "Empleado123"})
    assert response.status_code == 401


def test_delete_employee(client, company_headers):
    emp = create_employee(client, company_headers).json()["data"]
    assert client.delete(f"/api/employees/{emp['id']}", headers=company_headers).status_code == 200
    assert client.get(f"/api/employees/{emp['id']}", headers=company_headers).status_code == 404


# ---------------- QR ----------------


def test_qr_flow(client, company_headers):
    headers = approved_employee(client, company_headers)
    emp = {"id": client.get("/api/users/me", headers=headers).json()["data"]["employee"]["id"]}

    qr = client.get(f"/api/employees/{emp['id']}/qr", headers=company_headers)
    assert qr.status_code == 200
    assert qr.json()["data"]["image_base64"].startswith("data:image/png;base64,")

    content = _qr_content(emp["id"])
    token = content.removeprefix(QR_PREFIX)
    assert len(token) >= 40 and "employee" not in content.lower()
    ok = client.post("/api/verification/qr", json={"qr_content": content}, headers=headers)
    assert ok.json()["data"]["verified"] is True

    # Regenerar invalida el anterior.
    client.post(f"/api/employees/{emp['id']}/qr/regenerate", headers=company_headers)
    old = client.post("/api/verification/qr", json={"qr_content": content}, headers=headers)
    assert old.json()["data"] == {**old.json()["data"], "verified": False, "message": "QR no reconocido"}
    new = client.post("/api/verification/qr", json={"qr_content": _qr_content(emp["id"])}, headers=headers)
    assert new.json()["data"]["verified"] is True

    invalid = client.post("/api/verification/qr", json={"qr_content": "employee_id=1"}, headers=headers)
    assert invalid.json()["message"] == "QR inválido"

    history = client.get(f"/api/employees/{emp['id']}/verifications", headers=company_headers).json()["data"]
    assert len(history) == 4


def test_qr_of_other_employee_is_rejected(client, company_headers):
    headers = approved_employee(client, company_headers)
    other = create_employee(client, company_headers, number="EMP-002", email="ana@empresa.com").json()["data"]
    response = client.post("/api/verification/qr", json={"qr_content": _qr_content(other["id"])}, headers=headers)
    assert response.json()["data"]["verified"] is False


def test_login_rate_limit(client):
    for _ in range(10):
        client.post("/api/auth/login", json={"email": "x@empresa.com", "password": "bad"})
    response = client.post("/api/auth/login", json={"email": "x@empresa.com", "password": "bad"})
    assert response.status_code == 429
    assert "Retry-After" in response.headers
