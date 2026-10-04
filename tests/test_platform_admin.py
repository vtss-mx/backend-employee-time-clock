"""Plataforma multiempresa: el ADMIN da de alta empresas; cada empresa ve solo sus datos."""

import pytest

from tests.conftest import (
    COMPANY_EMAIL,
    COMPANY_PASSWORD,
    create_company,
    create_employee,
    curp_for,
    login,
    nss_for,
    rfc_for,
)

URL = "/api/admin/companies"


@pytest.fixture
def second_company(client, admin_headers):
    """Otra empresa de la plataforma y los encabezados de su administrador."""
    created = create_company(client, admin_headers)
    assert created.status_code == 201, created.text
    return created.json()["data"], login(client, "admin@panificadora.com", "Empresa1234")


# ---------------------------------------------------------------- roles


def test_only_the_platform_admin_uses_the_console(client, admin_headers, company_headers):
    assert client.get(URL, headers=admin_headers).status_code == 200
    assert client.get(URL, headers=company_headers).status_code == 403  # una empresa no ve las demás
    # El ADMIN no administra empleados de las empresas (privacidad por diseño).
    assert client.get("/api/employees", headers=admin_headers).status_code == 403
    me = client.get("/api/users/me", headers=admin_headers).json()["data"]
    assert me["role"] == "ADMIN" and me["company"] is None
    company_me = client.get("/api/users/me", headers=company_headers).json()["data"]
    assert company_me["company"]["name"] == "Mi empresa"


# ---------------------------------------------------------------- alta y edición


def test_create_company_with_its_first_admin(client, admin_headers):
    created = create_company(client, admin_headers, rfc="pno-120315-ab1", max_employees=50)
    assert created.status_code == 201
    company = created.json()["data"]
    assert company["rfc"] == "PNO120315AB1" and company["active"] is True and company["max_employees"] == 50
    assert "admins" not in company  # se piden paginados
    admins = client.get(f"{URL}/{company['id']}/admins", headers=admin_headers).json()["data"]["items"]
    assert [a["email"] for a in admins] == ["admin@panificadora.com"]
    assert company["admin_count"] == 1 and company["employee_count"] == 0

    # Un solo correo: el de la empresa es el de sus administradores (no hay correo de contacto aparte).
    assert "contact_email" not in company

    # Su administrador entra y la empresa nace con la política de verificación segura.
    headers = login(client, "admin@panificadora.com", "Empresa1234")
    policy = client.get("/api/settings/verification", headers=headers).json()["data"]
    assert policy["min_confidence"] == 0.99999 and policy["block_mask"] is True


def test_create_company_validations_and_duplicates(client, admin_headers):
    create_company(client, admin_headers)
    duplicate_rfc = create_company(client, admin_headers, admin_email="otro@x.com")
    assert duplicate_rfc.status_code == 409 and duplicate_rfc.json()["errors"][0]["field"] == "rfc"
    duplicate_email = create_company(client, admin_headers, rfc="ACM010101AB2")
    assert duplicate_email.status_code == 409 and duplicate_email.json()["errors"][0]["field"] == "admin_email"
    invalid = create_company(client, admin_headers, rfc="XAXX010101000", admin_email="a@b.com", phone="123")
    fields = {e["field"] for e in invalid.json()["errors"]}
    assert invalid.status_code == 422 and {"rfc", "phone"} <= fields
    weak = create_company(client, admin_headers, rfc="ACM010101AB2", admin_email="c@d.com", admin_password="corta")
    assert weak.status_code == 422 and weak.json()["errors"][0]["field"] == "admin_password"


def test_availability_for_company_forms(client, admin_headers):
    company_id = create_company(client, admin_headers).json()["data"]["id"]

    def check(field, value, **extra):
        params = {"field": field, "value": value, **extra}
        return client.get("/api/validation", params=params, headers=admin_headers).json()["data"]["code"]

    assert check("company_rfc", "PNO120315AB1") == "TAKEN"
    assert check("company_rfc", "PNO120315AB1", exclude_id=company_id) == "AVAILABLE"
    assert check("company_rfc", "ABC") == "INVALID_FORMAT"
    assert check("company_admin_email", COMPANY_EMAIL) == "TAKEN"
    assert check("company_admin_email", "nuevo@empresa.com") == "AVAILABLE"
    assert check("company_admin_email", "") == "EMPTY"
    # Datos de contacto (no únicos): solo el formato, con la misma regla que al guardar.
    assert check("company_phone", "662 123 4567") == "VALID"
    assert check("company_phone", "123") == "INVALID_FORMAT"


def test_list_search_counts_and_stats(client, admin_headers, company_headers, second_company):
    create_employee(client, company_headers)
    create_company(client, admin_headers, rfc="ACM010101AB2", admin_email="admin@acme.com", name="Acme Logística")

    listed = client.get(URL, headers=admin_headers).json()["data"]
    assert listed["total"] == 3
    names = [c["name"] for c in listed["items"]]
    assert names == sorted(names, key=str.lower)  # orden alfabético
    mine = next(c for c in listed["items"] if c["name"] == "Mi empresa")
    assert mine["employee_count"] == 1 and mine["admin_count"] == 1

    found = client.get(URL, params={"search": "acm010101"}, headers=admin_headers).json()["data"]
    assert [c["name"] for c in found["items"]] == ["Acme Logística"]
    assert client.get(URL, params={"active": "false"}, headers=admin_headers).json()["data"]["total"] == 0

    stats = client.get("/api/admin/stats", headers=admin_headers).json()["data"]
    assert stats == {"companies": 3, "active_companies": 3, "employees": 1, "company_admins": 3}


def test_update_company_and_clear_employee_limit(client, admin_headers, second_company):
    company, _ = second_company
    url = f"{URL}/{company['id']}"
    updated = client.put(url, json={"name": "Panificadora Norte", "max_employees": 5}, headers=admin_headers).json()[
        "data"
    ]
    assert updated["name"] == "Panificadora Norte" and updated["max_employees"] == 5
    cleared = client.put(url, json={"max_employees": None}, headers=admin_headers).json()["data"]
    assert cleared["max_employees"] is None and cleared["name"] == "Panificadora Norte"
    assert client.get(f"{URL}/999999", headers=admin_headers).status_code == 404


# ---------------------------------------------------------------- aislamiento entre empresas


def test_companies_are_isolated(client, company_headers, second_company):
    _, other = second_company
    mine = create_employee(client, company_headers).json()["data"]

    # La otra empresa puede usar el mismo número, RFC, CURP y NSS (son únicos por empresa)...
    same = client.post(
        "/api/employees",
        json={
            "first_name": "Juan",
            "last_name": "Pérez",
            "birth_date": "1990-05-10",
            "employee_number": "EMP-001",
            "rfc": rfc_for("EMP-001"),
            "curp": curp_for("EMP-001"),
            "nss": nss_for("EMP-001"),
            "phone": "6621234567",
            "email": "juan@panificadora.com",
            "password": "Empleado123",
        },
        headers=other,
    )
    assert same.status_code == 201, same.text

    # ...pero no ve ni toca a los empleados de otra empresa (404, como si no existieran).
    assert client.get(f"/api/employees/{mine['id']}", headers=other).status_code == 404
    assert client.put(f"/api/employees/{mine['id']}", json={"first_name": "X"}, headers=other).status_code == 404
    assert client.delete(f"/api/employees/{mine['id']}", headers=other).status_code == 404
    listed = client.get("/api/employees", headers=other).json()["data"]
    assert [e["email"] for e in listed["items"]] == ["juan@panificadora.com"]
    assert client.get("/api/employees", headers=company_headers).json()["data"]["total"] == 1

    # La política de verificación es de cada empresa.
    client.put("/api/settings/verification", json={"min_confidence": 0.9}, headers=other)
    assert client.get("/api/settings/verification", headers=company_headers).json()["data"]["min_confidence"] == 0.99999


def test_validation_inbox_is_per_company(client, company_headers, second_company):
    from tests.conftest import submit_enrollment

    _, other = second_company
    create_employee(client, company_headers)
    employee = login(client, "juan@empresa.com", "Empleado123")
    enrollment = submit_enrollment(client, employee).json()["data"]
    assert client.get("/api/enrollments", headers=company_headers).json()["data"]["total"] == 1
    assert client.get("/api/enrollments", headers=other).json()["data"]["total"] == 0
    assert client.get(f"/api/enrollments/{enrollment['enrollment_id']}", headers=other).status_code == 404
    approve = client.post(f"/api/enrollments/{enrollment['enrollment_id']}/approve", headers=other)
    assert approve.status_code == 404


# ---------------------------------------------------------------- estado y administradores


def test_deactivating_a_company_cuts_access_immediately(client, admin_headers, company_headers):
    create_employee(client, company_headers)
    company_id = client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]
    off = client.patch(f"{URL}/{company_id}/status", json={"active": False}, headers=admin_headers)
    assert off.status_code == 200 and off.json()["data"]["active"] is False

    assert client.get("/api/users/me", headers=company_headers).status_code == 401  # sesión cerrada
    for email, password in ((COMPANY_EMAIL, COMPANY_PASSWORD), ("juan@empresa.com", "Empleado123")):
        denied = client.post("/api/auth/login", json={"email": email, "password": password})
        assert denied.status_code == 401 and denied.json()["code"] == "COMPANY_INACTIVE"

    client.patch(f"{URL}/{company_id}/status", json={"active": True}, headers=admin_headers)
    assert login(client, COMPANY_EMAIL, COMPANY_PASSWORD)


def test_company_admins_management(client, admin_headers, second_company):
    company, first_admin = second_company
    url = f"{URL}/{company['id']}/admins"
    only = client.get(url, headers=admin_headers).json()["data"]["items"][0]["id"]
    last = client.patch(f"{url}/{only}/status", json={"active": False}, headers=admin_headers)
    assert last.status_code == 409 and last.json()["code"] == "LAST_COMPANY_ADMIN"

    added = client.post(
        url, json={"admin_email": "rh@panificadora.com", "admin_password": "Recursos123"}, headers=admin_headers
    )
    assert added.status_code == 201 and added.json()["data"]["admin_count"] == 2
    assert (
        client.post(
            url, json={"admin_email": "rh@panificadora.com", "admin_password": "Recursos123"}, headers=admin_headers
        ).status_code
        == 409
    )

    off = client.patch(f"{url}/{only}/status", json={"active": False}, headers=admin_headers)
    assert off.status_code == 200
    assert client.get("/api/users/me", headers=first_admin).status_code == 401  # su sesión se cerró
    assert login(client, "rh@panificadora.com", "Recursos123")
    other_company_user = client.patch(f"{url}/1/status", json={"active": False}, headers=admin_headers)
    assert other_company_user.status_code == 404  # no es administrador de esta empresa


def test_platform_admin_resets_a_company_admin_password(client, admin_headers, second_company):
    company, admin_session = second_company
    admins_url = f"{URL}/{company['id']}/admins"
    admin_id = client.get(admins_url, headers=admin_headers).json()["data"]["items"][0]["id"]
    url = f"{admins_url}/{admin_id}/password"
    found = client.get(f"{admins_url}/{admin_id}", headers=admin_headers)
    assert found.status_code == 200 and found.json()["data"]["email"] == "admin@panificadora.com"
    assert client.get(f"{admins_url}/1", headers=admin_headers).status_code == 404  # de otra empresa
    assert client.get(f"{admins_url}/{admin_id}", headers=admin_session).status_code == 403

    weak = client.put(url, json={"admin_password": "corta"}, headers=admin_headers)
    assert weak.status_code == 422 and weak.json()["errors"][0]["field"] == "admin_password"
    assert client.put(url, json={"admin_password": "Nueva12345"}, headers=admin_session).status_code == 403

    reset = client.put(url, json={"admin_password": "Nueva12345"}, headers=admin_headers)
    assert reset.status_code == 200 and reset.json()["code"] == "COMPANY_ADMIN_PASSWORD_RESET"
    assert client.get("/api/users/me", headers=admin_session).status_code == 401  # sesiones cerradas
    old = client.post("/api/auth/login", json={"email": "admin@panificadora.com", "password": "Empresa1234"})
    assert old.status_code == 401
    assert login(client, "admin@panificadora.com", "Nueva12345")
    # Un usuario de otra empresa no se puede tocar desde esta.
    other = client.put(
        f"{URL}/{company['id']}/admins/1/password", json={"admin_password": "Nueva12345"}, headers=admin_headers
    )
    assert other.status_code == 404


def test_employee_limit_of_the_plan(client, admin_headers, company_headers):
    company_id = client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]
    client.put(f"{URL}/{company_id}", json={"max_employees": 1}, headers=admin_headers)
    assert create_employee(client, company_headers).status_code == 201
    over = create_employee(client, company_headers, number="EMP-002", email="otro@empresa.com")
    assert over.status_code == 409 and over.json()["code"] == "EMPLOYEE_LIMIT_REACHED"


def test_admin_sees_the_employees_of_a_company_paginated_and_read_only(
    client, admin_headers, company_headers, second_company
):
    """El ADMIN ve la ficha de trabajo de los empleados de cada empresa, paginada y con búsqueda; sin
    datos fiscales, fecha de nacimiento ni nada biométrico."""
    for number, email in (
        ("EMP-001", "juan@empresa.com"),
        ("EMP-002", "ana@empresa.com"),
        ("EMP-003", "luis@empresa.com"),
    ):
        assert create_employee(client, company_headers, number=number, email=email).status_code == 201
    companies = client.get("/api/admin/companies", headers=admin_headers).json()["data"]["items"]
    other_id = second_company[0]["id"]
    mine = next(c["id"] for c in companies if c["id"] != other_id)
    url = f"/api/admin/companies/{mine}/employees"

    page = client.get(url, params={"size": 2}, headers=admin_headers).json()["data"]
    assert page["total"] == 3 and len(page["items"]) == 2
    item = page["items"][0]
    assert set(item) == {
        "id",
        "employee_number",
        "first_name",
        "last_name",
        "department_name",
        "email",
        "phone",
        "active",
        "face_status",
    }
    found = client.get(url, params={"search": "ana@"}, headers=admin_headers).json()["data"]
    assert [e["email"] for e in found["items"]] == ["ana@empresa.com"]
    assert client.get(url, params={"active": False}, headers=admin_headers).json()["data"]["total"] == 0

    other = client.get(f"/api/admin/companies/{other_id}/employees", headers=admin_headers).json()["data"]
    assert other["total"] == 0  # cada empresa con los suyos
    missing = client.get("/api/admin/companies/999999/employees", headers=admin_headers)
    assert missing.status_code == 404
    assert client.get(url, headers=company_headers).status_code == 403  # solo el ADMIN de la plataforma
