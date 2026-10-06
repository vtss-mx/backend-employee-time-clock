from tests.conftest import create_employee, login

# ---------------- Autenticación y roles ----------------


def test_login_and_me(client, company_headers):
    response = client.get("/api/users/me", headers=company_headers)
    assert response.status_code == 200
    assert response.json()["data"]["role"] == "COMPANY"
    # La webapp muestra fechas y horas en la zona del negocio (hora del Centro), no la del dispositivo.
    assert response.json()["data"]["timezone"] == "America/Mexico_City"


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
    files = [("images", ("f.jpg", b"face:juan", "image/jpeg"))]
    assert client.post("/api/verification/face", files=files, headers=company_headers).status_code == 403
    # El QR ya no se "verifica" desde la app del empleado: es dinámico y lo lee un validador.
    assert client.post("/api/verification/qr", json={"qr_content": "x"}, headers=company_headers).status_code == 404


# ---------------- Empleados ----------------


def test_create_employee(client, company_headers):
    response = create_employee(client, company_headers)
    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["employee_number"] == "EMP-001"
    # Sin QR fijo: el empleado genera su QR dinámico en su teléfono cuando lo necesita.
    assert body["has_face"] is False and "has_active_qr" not in body
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
    # Borrado lógico: su detalle se ve con la marca «Eliminado»; editarlo responde como si no existiera.
    assert client.get(f"/api/employees/{emp['id']}", headers=company_headers).json()["data"]["deleted_at"]
    assert (
        client.put(f"/api/employees/{emp['id']}", json={"first_name": "X"}, headers=company_headers).status_code == 404
    )


def test_login_rate_limit(client):
    for _ in range(10):
        client.post("/api/auth/login", json={"email": "x@empresa.com", "password": "bad"})
    response = client.post("/api/auth/login", json={"email": "x@empresa.com", "password": "bad"})
    assert response.status_code == 429
    assert "Retry-After" in response.headers
