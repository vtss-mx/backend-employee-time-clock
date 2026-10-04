"""Pantallas por rol (catalog.screens / catalog.role_screens): el menú del frontend y el permiso de
cada endpoint salen de la base de datos."""

from sqlalchemy import delete

from app.core.database import SessionLocal
from app.models import RoleScreen, Screen
from app.services.catalog_service import clear_catalog_cache
from app.services.navigation_service import AVAILABILITY
from tests.conftest import (
    approved_employee,
    create_company,
    create_employee,
    login,
    submit_enrollment,
)
from tests.test_validators import validator_headers


def _me(client, headers) -> dict:
    return client.get("/api/users/me", headers=headers).json()["data"]


def _codes(user: dict) -> list[str]:
    return [screen["code"] for screen in user["screens"]]


def _revoke(role: str, screen: Screen) -> None:
    with SessionLocal() as db:
        db.execute(delete(RoleScreen).where(RoleScreen.role_code == role, RoleScreen.screen_code == screen))
        db.commit()
    clear_catalog_cache()


def test_every_role_gets_its_screens_and_home(client, admin_headers, company_headers):
    admin = _me(client, admin_headers)
    assert _codes(admin) == ["ADMIN_DASHBOARD", "ADMIN_COMPANIES", "ADMIN_ERRORS", "PROFILE"]
    assert admin["home"] == "/admin/dashboard"
    assert admin["screens"][1] == {
        "code": "ADMIN_COMPANIES",
        "name": "Empresas",
        "short_name": None,
        "path": "/admin/companies",
        "icon": "Building2",
        "badge": None,
    }

    company = _me(client, company_headers)
    assert _codes(company) == [
        "COMPANY_DASHBOARD",
        "COMPANY_EMPLOYEES",
        "COMPANY_DEPARTMENTS",
        "COMPANY_VALIDATIONS",
        "COMPANY_VALIDATORS",
        "COMPANY_REPORTS",
        "COMPANY_SETTINGS",
        "COMPANY_API",
        "PROFILE",
    ]
    assert company["home"] == "/company/dashboard" and company["screens"][3]["badge"] == "PENDING_ENROLLMENTS"

    validator = _me(client, validator_headers(client, company_headers))
    assert _codes(validator) == ["VALIDATOR_CHECKPOINT", "PROFILE"]


def test_employee_screens_follow_the_state_of_their_face_registration(client, company_headers):
    create_employee(client, company_headers)
    headers = login(client, "juan@empresa.com", "Empleado123")
    assert _codes(_me(client, headers)) == ["EMPLOYEE_ENROLL", "PROFILE"]

    enrollment = submit_enrollment(client, headers).json()["data"]
    pending = _me(client, headers)
    assert _codes(pending) == ["EMPLOYEE_PENDING", "PROFILE"] and pending["home"] == "/employee/pending"

    client.post(f"/api/enrollments/{enrollment['enrollment_id']}/approve", headers=company_headers)
    approved = _me(client, headers)
    assert _codes(approved) == ["EMPLOYEE_VERIFY", "EMPLOYEE_QR", "PROFILE"]
    assert approved["home"] == "/employee/dashboard"


def test_login_and_refresh_carry_the_screens(client, company_headers):
    response = client.post("/api/auth/login", json={"email": "admin@empresa.com", "password": "Admin1234"})
    assert response.status_code == 200 and response.json()["data"]["user"]["home"] == "/company/dashboard"
    refreshed = client.post("/api/auth/refresh").json()["data"]["user"]
    assert _codes(refreshed)[0] == "COMPANY_DASHBOARD"


def test_revoking_a_screen_in_the_database_hides_it_and_blocks_its_endpoints(client, company_headers):
    assert client.get("/api/validators", headers=company_headers).status_code == 200
    _revoke("COMPANY", Screen.COMPANY_VALIDATORS)

    assert "COMPANY_VALIDATORS" not in _codes(_me(client, company_headers))
    assert client.get("/api/validators", headers=company_headers).status_code == 403
    # Otra pantalla que usa el mismo dato conserva el acceso: el resumen sigue contando empleados.
    _revoke("COMPANY", Screen.COMPANY_EMPLOYEES)
    assert client.get("/api/employees", headers=company_headers).status_code == 200  # COMPANY_DASHBOARD
    assert client.post("/api/employees", json={}, headers=company_headers).status_code == 403


def test_endpoints_of_a_screen_follow_its_grant(client, admin_headers, company_headers):
    approved = approved_employee(client, company_headers)
    assert client.post("/api/users/me/qr", headers=approved).status_code == 201
    _revoke("EMPLOYEE", Screen.EMPLOYEE_QR)
    assert client.post("/api/users/me/qr", headers=approved).status_code == 403

    _revoke("ADMIN", Screen.ADMIN_DASHBOARD)
    assert client.get("/api/admin/stats", headers=admin_headers).status_code == 403
    assert client.get("/api/admin/companies", headers=admin_headers).status_code == 200  # ADMIN_COMPANIES


def test_only_companies_without_employees_can_be_deleted(client, admin_headers, company_headers):
    created = create_company(client, admin_headers).json()["data"]
    url = f"/api/admin/companies/{created['id']}"
    other_admin = login(client, "admin@panificadora.com", "Empresa1234")
    assert client.delete(url, headers=other_admin).status_code == 403  # una empresa no elimina empresas

    deleted = client.delete(url, headers=admin_headers)
    assert deleted.status_code == 200 and deleted.json()["code"] == "COMPANY_DELETED"
    assert client.get(url, headers=admin_headers).status_code == 404
    assert client.get("/api/users/me", headers=other_admin).status_code == 401  # su cuenta ya no existe
    gone = client.post("/api/auth/login", json={"email": "admin@panificadora.com", "password": "Empresa1234"})
    assert gone.status_code == 401

    create_employee(client, company_headers)
    mine = _me(client, company_headers)["company"]["id"]
    kept = client.delete(f"/api/admin/companies/{mine}", headers=admin_headers)
    assert kept.status_code == 409 and kept.json()["code"] == "COMPANY_HAS_EMPLOYEES"
    assert client.delete("/api/admin/companies/999999", headers=admin_headers).status_code == 404


def test_availability_rules_name_real_screens():
    assert set(AVAILABILITY) <= set(Screen)
