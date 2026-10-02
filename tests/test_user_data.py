"""Datos de usuario en la BD: solo tres roles, teléfonos E.164, preferencias y cuenta recordada."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import RememberedAccount, User, UserRole
from tests.conftest import ADMIN_EMAIL, COMPANY_EMAIL, COMPANY_PASSWORD, create_company, create_employee, login

REMEMBER = settings.REMEMBER_COOKIE_NAME


# ---------------------------------------------------------------- roles


def test_only_the_defined_roles_exist():
    assert {role.value for role in UserRole} == {"ADMIN", "COMPANY", "EMPLOYEE", "VALIDATOR"}


@pytest.mark.parametrize(
    ("role", "company_id"),
    [
        ("SUPERVISOR", 1),  # rol inexistente
        ("ADMIN", 1),  # ADMIN es de la plataforma: sin empresa
        ("COMPANY", None),  # COMPANY siempre pertenece a UNA empresa
        ("VALIDATOR", None),  # el validador también
        ("EMPLOYEE", 1),  # EMPLOYEE trabaja en empresas a través de sus empleos, no de su cuenta
    ],
)
def test_database_rejects_invalid_roles(role, company_id):
    """La BD lo garantiza aunque alguien escriba fuera de la API (SQL directo)."""
    with SessionLocal() as db:
        company_id = company_id and db.query(User.company_id).filter(User.email == COMPANY_EMAIL).scalar()
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO users (email, password_hash, role, active, company_id, preferences) "
                    "VALUES (:email, 'x', :role, true, :company_id, '{}')"
                ),
                {"email": f"{role.lower()}@x.com", "role": role, "company_id": company_id},
            )
        db.rollback()


# ---------------------------------------------------------------- teléfonos con lada


def test_phones_are_stored_with_country_code(client, company_headers, admin_headers):
    created = create_employee(client, company_headers, phone="662 123 4567").json()["data"]
    assert created["phone"] == "+526621234567"  # sin lada → México

    url = f"/api/employees/{created['id']}"
    us = client.put(url, json={"phone": "+1 (415) 555-2671"}, headers=company_headers).json()["data"]
    assert us["phone"] == "+14155552671"
    invalid = client.put(url, json={"phone": "+52 662 123 456"}, headers=company_headers)
    assert invalid.status_code == 422 and invalid.json()["errors"][0]["field"] == "phone"
    assert "+52" in invalid.json()["errors"][0]["message"]

    company = create_company(client, admin_headers, phone="+34 612 34 56 78").json()["data"]
    assert company["phone"] == "+34612345678"


# ---------------------------------------------------------------- preferencias en la BD


def test_preferences_live_in_the_database(client, company_headers):
    me = client.get("/api/users/me", headers=company_headers).json()["data"]
    assert me["preferences"] == {"sidebar_collapsed": False}

    saved = client.patch("/api/users/me/preferences", json={"sidebar_collapsed": True}, headers=company_headers)
    assert saved.status_code == 200 and saved.json()["data"] == {"sidebar_collapsed": True}
    with SessionLocal() as db:
        assert db.query(User.preferences).filter(User.email == COMPANY_EMAIL).scalar() == {"sidebar_collapsed": True}

    # Siguen al usuario en otro dispositivo (otra sesión).
    other_device = login(client, COMPANY_EMAIL, COMPANY_PASSWORD)
    assert client.get("/api/users/me", headers=other_device).json()["data"]["preferences"]["sidebar_collapsed"]

    unknown = client.patch("/api/users/me/preferences", json={"theme": "dark"}, headers=other_device)
    assert unknown.status_code == 422
    assert client.patch("/api/users/me/preferences", json={"sidebar_collapsed": False}).status_code == 401


# ---------------------------------------------------------------- "Recordar mi cuenta"


def _login(client, *, remember: bool, email=COMPANY_EMAIL, password=COMPANY_PASSWORD):
    response = client.post("/api/auth/login", json={"email": email, "password": password, "remember": remember})
    assert response.status_code == 200, response.text
    return response


def test_remembered_account_lives_in_the_database(client):
    assert client.get("/api/auth/remembered").json()["data"] is None

    response = _login(client, remember=True)
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith(f"{REMEMBER}="))
    assert "HttpOnly" in cookie and "Path=/api/auth" in cookie and "SameSite=strict" in cookie
    assert f"Max-Age={settings.REMEMBER_ACCOUNT_DAYS * 86400}" in cookie
    assert COMPANY_EMAIL not in cookie  # el dispositivo solo guarda un token opaco

    with SessionLocal() as db:
        stored = db.query(RememberedAccount).one()
        assert stored.user.email == COMPANY_EMAIL and stored.token_hash not in cookie

    # Tras cerrar sesión, el login de ese dispositivo muestra el correo ya escrito.
    client.post("/api/auth/logout")
    assert client.get("/api/auth/remembered").json()["data"] == {"email": COMPANY_EMAIL}

    # Un token alterado no revela nada.
    client.cookies.set(REMEMBER, cookie.split(";")[0].split("=", 1)[1][:-2] + "xx", path="/api/auth")
    assert client.get("/api/auth/remembered").json()["data"] is None


def test_unchecking_or_using_another_account_forgets_the_device(client, admin_headers):
    _login(client, remember=True)
    _login(client, remember=False)  # desmarcar la casilla olvida la cuenta
    assert client.get("/api/auth/remembered").json()["data"] is None
    with SessionLocal() as db:
        assert db.query(RememberedAccount).count() == 0

    _login(client, remember=True)
    forgotten = client.delete("/api/auth/remembered")  # "Usar otra cuenta"
    assert forgotten.status_code == 200 and client.get("/api/auth/remembered").json()["data"] is None

    # Otra cuenta en el mismo dispositivo reemplaza a la anterior (un solo registro por dispositivo).
    _login(client, remember=True)
    _login(client, remember=True, email=ADMIN_EMAIL, password="Plataforma1234")
    assert client.get("/api/auth/remembered").json()["data"] == {"email": ADMIN_EMAIL}
    with SessionLocal() as db:
        assert db.query(RememberedAccount).count() == 1


def test_deactivated_user_is_not_remembered(client, company_headers):
    create_employee(client, company_headers)
    _login(client, remember=True, email="juan@empresa.com", password="Empleado123")
    employee_id = client.get("/api/employees", headers=company_headers).json()["data"]["items"][0]["id"]
    client.patch(f"/api/employees/{employee_id}/status", json={"active": False}, headers=company_headers)
    assert client.get("/api/auth/remembered").json()["data"] is None


# ---------------------------------------------------------------- Swagger


def test_swagger_is_reachable_from_backend_root_and_api_prefix(client):
    for url in ("/", "/api/docs"):
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 307 and response.headers["location"] == "/docs"
    docs = client.get("/docs")
    assert docs.status_code == 200 and "swagger-ui" in docs.text
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/api/auth/remembered", "/api/users/me/preferences", "/api/admin/companies"} <= set(paths)
