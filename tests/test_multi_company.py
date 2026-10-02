"""Una persona (correo y teléfono únicos) puede trabajar en varias empresas y elige a cuál entrar."""

import pytest

from tests.conftest import (
    COMPANY_EMAIL,
    DESKTOP_UA,
    create_company,
    create_employee,
    curp_for,
    login,
    nss_for,
    phone_for,
    rfc_for,
)
from tests.test_policy import set_policy

EMAIL = "juan@empresa.com"
PASSWORD = "Empleado123"
PHONE = "+52" + phone_for("EMP-001")


@pytest.fixture
def two_companies(client, admin_headers, company_headers):
    """ "Mi empresa" (A) y "Panificadora" (B) con la misma persona como empleado de ambas."""
    other = create_company(client, admin_headers).json()["data"]
    other_headers = login(client, "admin@panificadora.com", "Empresa1234")
    in_a = create_employee(client, company_headers).json()["data"]
    linked = create_employee(client, other_headers, number="PAN-7", phone=PHONE)  # sin contraseña: se vincula
    assert linked.status_code == 201, linked.text
    company_a = client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]
    return {
        "a": company_a,
        "b": other["id"],
        "a_headers": company_headers,
        "b_headers": other_headers,
        "in_a": in_a,
        "in_b": linked.json()["data"],
    }


def _login(client, **headers):
    response = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return {"Authorization": f"Bearer {data['access_token']}"}, data["user"]


def test_same_person_is_linked_not_duplicated(client, two_companies):
    in_a, in_b = two_companies["in_a"], two_companies["in_b"]
    assert in_a["user_id"] == in_b["user_id"] and in_b["shared_account"] is True
    assert in_b["email"] == EMAIL and in_b["phone"] == PHONE
    # Al vincularse no cambió su contraseña (la empresa B no la conoce).
    assert _login(client)[1]["memberships"][1]["company"]["name"] == "Panificadora del Norte"
    # Ya es empleado de B: un segundo alta en B se rechaza.
    again = create_employee(client, two_companies["b_headers"], number="PAN-8", phone=PHONE)
    assert again.status_code == 409 and again.json()["errors"][0]["field"] == "email"


def test_email_and_phone_are_unique_per_person(client, admin_headers, company_headers):
    create_employee(client, company_headers)
    create_company(client, admin_headers)
    other = login(client, "admin@panificadora.com", "Empresa1234")

    mismatch = create_employee(client, other, number="PAN-1", phone="+52 662 765 4321")  # mismo correo, otro teléfono
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "ACCOUNT_PHONE_MISMATCH"
    taken_phone = create_employee(client, other, number="PAN-2", email="otra@persona.com", phone=PHONE)
    assert taken_phone.status_code == 409 and taken_phone.json()["code"] == "PHONE_TAKEN"
    admin_email = create_employee(client, other, number="PAN-3", email=COMPANY_EMAIL, phone="+52 662 765 4321")
    assert admin_email.status_code == 409 and admin_email.json()["errors"][0]["field"] == "email"

    def check(headers, field, value):
        params = {"field": field, "value": value}
        return client.get("/api/validation", params=params, headers=headers).json()["data"]["code"]

    assert check(other, "email", EMAIL) == "LINKABLE"  # persona de otra empresa
    assert check(other, "phone", PHONE) == "LINKABLE"
    assert check(company_headers, "email", EMAIL) == "TAKEN"  # ya trabaja aquí
    assert check(other, "email", COMPANY_EMAIL) == "TAKEN"  # administrador
    assert check(other, "phone", "+52 662 765 4321") == "AVAILABLE"


def test_new_person_requires_a_password(client, company_headers):
    body = {
        "first_name": "Ana",
        "last_name": "Ruiz",
        "birth_date": "1990-05-10",
        "employee_number": "EMP-9",
        "rfc": rfc_for("EMP-9"),
        "curp": curp_for("EMP-9"),
        "nss": nss_for("EMP-9"),
        "phone": "+52 662 111 2233",
        "email": "ana@empresa.com",
    }
    response = client.post("/api/employees", json=body, headers=company_headers)
    assert response.status_code == 422 and response.json()["code"] == "PASSWORD_REQUIRED"
    assert response.json()["errors"][0]["field"] == "password"
    # Una contraseña vacía equivale a no enviarla (no se valida como contraseña débil).
    empty = client.post("/api/employees", json={**body, "password": ""}, headers=company_headers)
    assert empty.json()["code"] == "PASSWORD_REQUIRED"


def test_login_with_several_companies_requires_choosing_one(client, two_companies):
    headers, user = _login(client)
    assert user["company"] is None and user["employee"] is None
    assert {m["company"]["id"] for m in user["memberships"]} == {two_companies["a"], two_companies["b"]}
    pending = client.get("/api/users/me/qr", headers=headers)
    assert pending.status_code == 409 and pending.json()["code"] == "COMPANY_SELECTION_REQUIRED"

    chosen = client.post("/api/auth/company", json={"company_id": two_companies["b"]}, headers=headers)
    assert chosen.status_code == 200, chosen.text
    data = chosen.json()["data"]
    assert data["company"]["id"] == two_companies["b"] and data["employee"]["id"] == two_companies["in_b"]["id"]
    assert client.get("/api/users/me", headers=headers).json()["data"]["company"]["id"] == two_companies["b"]

    # Cambia de empresa sin volver a iniciar sesión; nunca a una donde no trabaja.
    switched = client.post("/api/auth/company", json={"company_id": two_companies["a"]}, headers=headers)
    assert switched.json()["data"]["employee"]["id"] == two_companies["in_a"]["id"]
    assert client.post("/api/auth/company", json={"company_id": 999}, headers=headers).status_code == 404
    # La renovación conserva la empresa elegida.
    refreshed = client.post("/api/auth/refresh").json()["data"]["user"]
    assert refreshed["company"]["id"] == two_companies["a"]


def test_deactivating_in_one_company_keeps_the_other(client, two_companies, admin_headers):
    headers, _ = _login(client)
    client.post("/api/auth/company", json={"company_id": two_companies["b"]}, headers=headers)
    url = f"/api/employees/{two_companies['in_b']['id']}/status"
    assert client.patch(url, json={"active": False}, headers=two_companies["b_headers"]).status_code == 200
    assert client.get("/api/users/me", headers=headers).status_code == 401  # su sesión en B se cerró

    # Sigue trabajando en A: con un solo empleo utilizable entra directo a A.
    _, user = _login(client)
    assert user["company"]["id"] == two_companies["a"]
    # Y si la plataforma desactiva A, ya no tiene a dónde entrar.
    client.patch(f"/api/admin/companies/{two_companies['a']}/status", json={"active": False}, headers=admin_headers)
    denied = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert denied.status_code == 401 and denied.json()["code"] == "COMPANY_INACTIVE"


def test_a_company_cannot_change_a_shared_account(client, two_companies):
    url = f"/api/employees/{two_companies['in_a']['id']}"
    for change in ({"email": "nuevo@correo.com"}, {"phone": "+52 662 999 8877"}, {"password": "Cambio1234"}):
        response = client.put(url, json=change, headers=two_companies["a_headers"])
        assert response.status_code == 409 and response.json()["code"] == "SHARED_ACCOUNT", change
    # Sus datos laborales sí son de cada empresa.
    renamed = client.put(url, json={"first_name": "Juan Carlos"}, headers=two_companies["a_headers"])
    assert renamed.status_code == 200 and renamed.json()["data"]["first_name"] == "Juan Carlos"


def test_removing_from_one_company_keeps_the_account(client, two_companies):
    url_b = f"/api/employees/{two_companies['in_b']['id']}"
    assert client.delete(url_b, headers=two_companies["b_headers"]).status_code == 200
    _, user = _login(client)
    assert [m["company"]["id"] for m in user["memberships"]] == [two_companies["a"]]

    # Su último empleo: la cuenta se elimina por completo.
    client.delete(f"/api/employees/{two_companies['in_a']['id']}", headers=two_companies["a_headers"])
    assert client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 401


def test_each_company_applies_its_own_device_policy(client, two_companies):
    set_policy(client, two_companies["b_headers"], employee_mobile_only=True)
    set_policy(client, two_companies["a_headers"], employee_mobile_only=False)
    headers, _ = _login(client, **{"User-Agent": DESKTOP_UA})  # aún sin empresa: no se exige dispositivo
    desktop = {**headers, "User-Agent": DESKTOP_UA}
    blocked = client.post("/api/auth/company", json={"company_id": two_companies["b"]}, headers=desktop)
    assert blocked.status_code == 403 and blocked.json()["code"] == "MOBILE_DEVICE_REQUIRED"
    allowed = client.post("/api/auth/company", json={"company_id": two_companies["a"]}, headers=desktop)
    assert allowed.status_code == 200
