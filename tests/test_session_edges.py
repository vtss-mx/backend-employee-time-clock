"""Sesiones y reglas de la cuenta en los casos límite: tokens con datos que no cuadran, cierres
repetidos, relojes desfasados entre instancias, cuentas o empleos desactivados y contraseñas con
parámetros de hash viejos."""

from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher

from app.core import tokens
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.passwords import password_needs_rehash, verify_password
from app.models import AuthSession, SessionRevocationReason, User
from app.repositories.user_repository import UserRepository
from tests.conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    COMPANY_EMAIL,
    COMPANY_PASSWORD,
    create_company,
    create_employee,
    login,
)
from tests.test_multi_company import PHONE

ME = "/api/users/me"
EMPLOYEE_EMAIL, EMPLOYEE_PASSWORD = "juan@empresa.com", "Empleado123"


def _token(headers: dict[str, str]) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


def _resigned(headers: dict[str, str], *, drop: str | None = None, **changes: str) -> dict[str, str]:
    """El mismo access token con otros datos, firmado con la llave REAL del servidor: así solo se
    prueba la validación de su contenido (la firma es válida)."""
    token = _token(headers)
    claims = {**jwt.decode(token, options={"verify_signature": False}), **changes}
    claims.pop(drop or "", None)
    current, _ = tokens._keys()
    signed = jwt.encode(claims, current.private, algorithm="ES256", headers=jwt.get_unverified_header(token))
    return {"Authorization": f"Bearer {signed}"}


def _user(db, email: str) -> User:
    user = UserRepository(db).get_by_email(email)
    assert user is not None
    return user


def test_refresh_of_a_session_that_does_not_exist_is_rejected(client):
    client.cookies.set(settings.REFRESH_COOKIE_NAME, "sesion-inventada.secreto", path="/api/auth")
    response = client.post("/api/auth/refresh")
    assert response.status_code == 401 and response.json()["code"] == "SESSION_INVALID"


def test_a_signed_token_with_a_malformed_identity_is_rejected(client, company_headers):
    for forged in (_resigned(company_headers, sub="no-es-numero"), _resigned(company_headers, drop="sid")):
        response = client.get(ME, headers=forged)
        assert response.status_code == 401 and response.json()["code"] == "TOKEN_INVALID"
    assert client.get(ME, headers=company_headers).status_code == 200  # el token original sigue sirviendo


def test_the_token_role_must_match_the_account(client, company_headers):
    """Aunque la firma sea válida, un token que dice otro rol que el de la cuenta no sirve."""
    escalated = client.get("/api/admin/companies", headers=_resigned(company_headers, role="ADMIN"))
    assert escalated.status_code == 401 and escalated.json()["code"] == "TOKEN_INVALID"


def test_logging_out_twice_with_the_same_token_is_idempotent(client, company_headers):
    session_id = jwt.decode(_token(company_headers), options={"verify_signature": False})["sid"]
    client.cookies.clear()  # solo el Bearer (otra app sin la cookie)
    assert client.post("/api/auth/logout", headers=company_headers).status_code == 200
    with SessionLocal() as db:
        first = db.get(AuthSession, session_id)
        assert first is not None and first.revoked_reason == SessionRevocationReason.LOGOUT
        closed_at = first.revoked_at
    again = client.post("/api/auth/logout", headers=company_headers)  # p. ej. el reintento de la red
    assert again.status_code == 200 and again.json()["code"] == "LOGGED_OUT"
    with SessionLocal() as db:
        assert db.get(AuthSession, session_id).revoked_at == closed_at  # el cierre original no cambia


def test_a_new_session_never_closes_itself_when_another_clock_runs_ahead(client):
    """Otra instancia con el reloj adelantado creó una sesión "más nueva" que la que se está creando:
    al aplicar el límite de sesiones, la nueva nunca se cierra a sí misma."""
    ahead = datetime.now(UTC) + timedelta(minutes=1)
    with SessionLocal() as db:
        db.add(
            AuthSession(
                id="reloj-adelantado",
                user_id=_user(db, COMPANY_EMAIL).id,
                refresh_hash="0" * 64,
                created_at=ahead,
                last_used_at=ahead,
                expires_at=ahead + timedelta(hours=1),
            )
        )
        db.commit()
    headers = login(client, COMPANY_EMAIL, COMPANY_PASSWORD)
    assert client.get(ME, headers=headers).status_code == 200


def test_a_deactivated_company_admin_cannot_sign_in_until_reactivated(client, admin_headers):
    company = create_company(client, admin_headers).json()["data"]
    admins = f"/api/admin/companies/{company['id']}/admins"
    extra = {"admin_email": "rh@panificadora.com", "admin_password": "Recursos123"}
    assert client.post(admins, json=extra, headers=admin_headers).status_code == 201
    first = client.get(admins, headers=admin_headers).json()["data"]["items"][0]["id"]

    client.patch(f"{admins}/{first}/status", json={"active": False}, headers=admin_headers)
    denied = client.post("/api/auth/login", json={"email": "admin@panificadora.com", "password": "Empresa1234"})
    assert denied.status_code == 401 and denied.json()["code"] == "USER_INACTIVE"

    back = client.patch(f"{admins}/{first}/status", json={"active": True}, headers=admin_headers)
    assert back.status_code == 200 and back.json()["data"]["admin_count"] == 2
    assert login(client, "admin@panificadora.com", "Empresa1234")


def _employed_in_two(client, admin_headers, company_headers) -> tuple[int, dict[str, str], int]:
    """La misma persona trabaja en "Mi empresa" y en otra: (id de la otra, su administrador, empleo ahí)."""
    other_id = create_company(client, admin_headers).json()["data"]["id"]
    other_headers = login(client, "admin@panificadora.com", "Empresa1234")
    create_employee(client, company_headers)
    linked = create_employee(client, other_headers, number="PAN-7", phone=PHONE).json()["data"]
    return other_id, other_headers, linked["id"]


def _choose(client, headers: dict[str, str], company_id: int):
    return client.post("/api/auth/company", json={"company_id": company_id}, headers=headers)


def test_an_employee_cannot_enter_a_company_where_its_job_was_deactivated(client, admin_headers, company_headers):
    other_id, other_headers, job = _employed_in_two(client, admin_headers, company_headers)
    client.patch(f"/api/employees/{job}/status", json={"active": False}, headers=other_headers)
    headers = login(client, EMPLOYEE_EMAIL, EMPLOYEE_PASSWORD)  # entra directo a la única que le queda

    denied = _choose(client, headers, other_id)
    assert denied.status_code == 401 and denied.json()["code"] == "USER_INACTIVE"
    assert client.get(ME, headers=headers).json()["data"]["company"]["name"] == "Mi empresa"  # sigue donde estaba


def test_an_employee_cannot_enter_a_deactivated_company(client, admin_headers, company_headers):
    other_id, _, _ = _employed_in_two(client, admin_headers, company_headers)
    client.patch(f"/api/admin/companies/{other_id}/status", json={"active": False}, headers=admin_headers)
    headers = login(client, EMPLOYEE_EMAIL, EMPLOYEE_PASSWORD)

    denied = _choose(client, headers, other_id)
    assert denied.status_code == 401 and denied.json()["code"] == "COMPANY_INACTIVE"


def test_a_password_hashed_with_old_parameters_is_upgraded_at_sign_in(client):
    """Si cambian los parámetros de Argon2, cada cuenta se actualiza sola en su siguiente inicio de sesión."""
    legacy = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(ADMIN_PASSWORD)
    with SessionLocal() as db:
        _user(db, ADMIN_EMAIL).password_hash = legacy
        db.commit()
    assert password_needs_rehash(legacy)

    assert login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    with SessionLocal() as db:
        upgraded = _user(db, ADMIN_EMAIL).password_hash
    assert upgraded != legacy and not password_needs_rehash(upgraded)
    assert verify_password(ADMIN_PASSWORD, upgraded)
