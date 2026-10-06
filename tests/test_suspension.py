"""Empresa suspendida (falta de pago o decisión del ADMIN): sus sesiones se cierran al momento y nadie de
ella inicia sesión ni opera (403 COMPANY_SUSPENDED), incluida su llave de la API; un empleado que también
trabaja en otra empresa sigue entrando a esa otra; reactivarla devuelve el acceso."""

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import User
from tests.billing_support import billing_url
from tests.conftest import COMPANY_EMAIL, COMPANY_PASSWORD, create_company, create_employee, login, phone_for
from tests.test_api_keys import call, new_key
from tests.test_validators import PASSWORD as VALIDATOR_PASSWORD
from tests.test_validators import validator_headers

EMPLOYEE = {"email": "juan@empresa.com", "password": "Empleado123"}


def _company_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(User.company_id).where(User.email == COMPANY_EMAIL)))


def _suspend(client, admin_headers, company_id: int) -> None:
    response = client.post(
        billing_url(company_id, "/suspend"), json={"reason": "Prueba de suspensión"}, headers=admin_headers
    )
    assert response.status_code == 200, response.text


def test_every_account_of_a_suspended_company_is_cut_off(client, admin_headers, company_headers):
    validator = validator_headers(client, company_headers, mode="QR")
    assert create_employee(client, company_headers).status_code == 201
    employee = login(client, **EMPLOYEE)
    secret = new_key(client, company_headers)["secret"]
    company_id = _company_id()
    _suspend(client, admin_headers, company_id)

    # Las sesiones abiertas se cerraron: la siguiente petición lo dice (401 con el código de suspensión).
    for headers in (company_headers, validator, employee):
        response = client.get("/api/users/me", headers=headers)
        assert response.status_code == 401 and response.json()["code"] == "COMPANY_SUSPENDED"
        assert "Contacta al administrador de la plataforma" in response.json()["message"]
    refreshed = client.post("/api/auth/refresh")
    assert refreshed.status_code == 401 and refreshed.json()["code"] == "COMPANY_SUSPENDED"
    # Y nadie vuelve a entrar (403: la sesión no es el problema).
    for email, password in (
        (COMPANY_EMAIL, COMPANY_PASSWORD),
        ("recepcion@empresa.com", VALIDATOR_PASSWORD),
        (EMPLOYEE["email"], EMPLOYEE["password"]),
    ):
        denied = client.post("/api/auth/login", json={"email": email, "password": password})
        assert denied.status_code == 403 and denied.json()["code"] == "COMPANY_SUSPENDED", email
    api = call(client, secret, "/employees")
    assert api.status_code == 403 and api.json()["code"] == "COMPANY_SUSPENDED"
    # El ADMIN de la plataforma no pertenece a ninguna empresa: sigue operando.
    assert client.get("/api/admin/companies", headers=admin_headers).status_code == 200

    # Reactivarla devuelve el acceso de inmediato.
    assert client.post(billing_url(company_id, "/reactivate"), json={}, headers=admin_headers).status_code == 200
    assert client.get("/api/users/me", headers=login(client, COMPANY_EMAIL, COMPANY_PASSWORD)).status_code == 200
    assert call(client, secret, "/employees").status_code == 200


def test_an_employee_of_two_companies_keeps_the_other_one(client, admin_headers, company_headers):
    other = create_company(client, admin_headers).json()["data"]
    other_headers = login(client, "admin@panificadora.com", "Empresa1234")
    assert create_employee(client, company_headers).status_code == 201
    linked = create_employee(client, other_headers, number="PAN-7", phone="+52" + phone_for("EMP-001"))
    assert linked.status_code == 201
    own = _company_id()
    _suspend(client, admin_headers, own)

    # Con un solo empleo utilizable entra directo a la otra empresa.
    response = client.post("/api/auth/login", json=EMPLOYEE)
    assert response.status_code == 200
    user = response.json()["data"]["user"]
    assert user["company"]["id"] == other["id"]
    headers = {"Authorization": f"Bearer {response.json()['data']['access_token']}"}
    chosen = client.post("/api/auth/company", json={"company_id": own}, headers=headers)
    assert chosen.status_code == 403 and chosen.json()["code"] == "COMPANY_SUSPENDED"

    # Si la otra también se suspende, ya no tiene a dónde entrar: se le dice que su empresa está suspendida.
    _suspend(client, admin_headers, other["id"])
    denied = client.post("/api/auth/login", json=EMPLOYEE)
    assert denied.status_code == 403 and denied.json()["code"] == "COMPANY_SUSPENDED"
