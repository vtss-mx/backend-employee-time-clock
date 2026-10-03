"""JWT ES256, sesiones revocables y rotación de refresh tokens con detección de reutilización."""

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import update

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import AuthenticationError
from app.core.tokens import ACCESS_TOKEN_TYPE, decode_access_token, jwks
from app.models import AuthSession
from tests.conftest import COMPANY_EMAIL, COMPANY_PASSWORD, create_employee

COOKIE = settings.REFRESH_COOKIE_NAME


def _login(client, email=COMPANY_EMAIL, password=COMPANY_PASSWORD):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


def _bearer(response) -> dict[str, str]:
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_login_issues_es256_token_valid_12_hours_and_httponly_cookie(client):
    response = _login(client)
    data = response.json()["data"]
    header = jwt.get_unverified_header(data["access_token"])
    claims = jwt.decode(data["access_token"], options={"verify_signature": False})

    assert header["alg"] == "ES256" and header["typ"] == ACCESS_TOKEN_TYPE and header["kid"]
    assert claims["exp"] - claims["iat"] == 12 * 3600 == data["expires_in"]
    assert {"iss", "aud", "sub", "sid", "jti", "role", "nbf"} <= set(claims)
    assert claims["sid"] == data["session_id"] and data["token_type"] == "Bearer"
    cookie = response.headers["set-cookie"]
    assert f"{COOKIE}=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/api/auth" in cookie
    assert "refresh" not in data  # el refresh token nunca viaja en el cuerpo


def _refresh_cookie(response) -> str:
    return next(c for c in response.headers.get_list("set-cookie") if c.startswith(f"{COOKIE}="))


def test_remember_me_controls_cookie_persistence(client):
    """Sin "Recordar mi cuenta" la cookie es de sesión del navegador; con ella dura lo que la sesión."""
    browser_only = _refresh_cookie(_login(client))
    assert "Max-Age" not in browser_only and "expires" not in browser_only.lower()
    refreshed = client.post("/api/auth/refresh")
    assert refreshed.status_code == 200 and "Max-Age" not in _refresh_cookie(refreshed)

    remembered = client.post(
        "/api/auth/login", json={"email": COMPANY_EMAIL, "password": COMPANY_PASSWORD, "remember": True}
    )
    max_age = int(_refresh_cookie(remembered).split("Max-Age=")[1].split(";")[0])
    assert max_age == remembered.json()["data"]["expires_in"] == 12 * 3600
    # Al renovar se mantiene la elección, sin extender la sesión.
    renewed = client.post("/api/auth/refresh")
    assert 0 < int(_refresh_cookie(renewed).split("Max-Age=")[1].split(";")[0]) <= max_age


def test_jwks_verifies_issued_tokens(client):
    token = _login(client).json()["data"]["access_token"]
    body = client.get("/api/auth/jwks").json()
    key = body["data"]["keys"][0]
    assert key["kty"] == "EC" and key["alg"] == "ES256" and "d" not in key  # nunca la parte privada
    public = jwt.PyJWK(key).key
    claims = jwt.decode(token, public, algorithms=["ES256"], audience=settings.JWT_AUDIENCE)
    assert claims["iss"] == settings.JWT_ISSUER and jwks()["keys"][0]["kid"] == key["kid"]


@pytest.mark.parametrize(
    ("alg", "key", "changes"),
    [
        ("none", None, {}),  # sin firma
        ("HS256", "x" * 32, {}),  # algoritmo distinto ("alg confusion")
        ("HS256", "x" * 32, {"role": "COMPANY", "sub": "999"}),  # escalada de privilegios
    ],
)
def test_forged_tokens_are_rejected(client, alg, key, changes):
    token = _login(client).json()["data"]["access_token"]
    header = jwt.get_unverified_header(token)
    claims = {**jwt.decode(token, options={"verify_signature": False}), **changes}
    forged = jwt.encode(claims, key, algorithm=alg, headers={"kid": header["kid"], "typ": header["typ"]})
    response = client.get("/api/users/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401 and response.json()["code"] == "TOKEN_INVALID"


@pytest.mark.parametrize("claim", ["aud", "iss"])
def test_tokens_for_other_audience_or_issuer_are_rejected(claim):
    from app.core import tokens

    current, _ = tokens._keys()
    claims = {
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "sub": "1",
        "sid": "s",
        "jti": "j",
        "role": "COMPANY",
        "iat": datetime.now(UTC),
        "nbf": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(hours=1),
        claim: "otro-valor",
    }
    token = jwt.encode(
        claims, current.private, algorithm="ES256", headers={"kid": current.kid, "typ": ACCESS_TOKEN_TYPE}
    )
    with pytest.raises(AuthenticationError) as exc:
        decode_access_token(token)
    assert exc.value.code == "TOKEN_INVALID"


def test_expired_token_returns_token_expired(client, monkeypatch):
    token = _login(client).json()["data"]["access_token"]
    future = datetime.now(UTC) + timedelta(hours=12, minutes=5)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(jwt.api_jwt, "datetime", FrozenDatetime)
    with pytest.raises(AuthenticationError) as exc:
        decode_access_token(token)
    assert exc.value.code == "TOKEN_EXPIRED"


def test_refresh_rotates_and_detects_reuse(client, monkeypatch):
    login = _login(client)
    first_cookie = client.cookies.get(COOKIE)

    refreshed = client.post("/api/auth/refresh")
    assert refreshed.status_code == 200 and refreshed.json()["code"] == "TOKEN_REFRESHED"
    second_cookie = client.cookies.get(COOKIE)
    assert second_cookie and second_cookie != first_cookie
    assert refreshed.json()["data"]["session_id"] == login.json()["data"]["session_id"]

    # Dentro de la ventana de gracia (otra pestaña con la cookie anterior): se permite sin rotar.
    client.cookies.set(COOKIE, first_cookie, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 200

    # Fuera de la ventana: reutilizar el token anterior se considera robo → sesión revocada.
    monkeypatch.setattr(settings, "REFRESH_REUSE_GRACE_SECONDS", 0)
    time.sleep(0.01)
    client.cookies.set(COOKIE, first_cookie, path="/api/auth")
    reused = client.post("/api/auth/refresh")
    assert reused.status_code == 401 and reused.json()["code"] == "REFRESH_TOKEN_REUSED"
    # El access token de esa sesión deja de funcionar de inmediato y el token vigente también.
    assert client.get("/api/users/me", headers=_bearer(login)).json()["code"] == "SESSION_REVOKED"
    client.cookies.set(COOKIE, second_cookie, path="/api/auth")
    assert client.post("/api/auth/refresh").json()["code"] == "SESSION_INVALID"


def test_session_lasts_12h_and_refresh_never_extends_it(client):
    """La sesión vence 12 h después del login; renovar (recargar la página) no la alarga."""
    login = _login(client).json()["data"]
    assert login["expires_in"] == 720 * 60
    login_expiry = datetime.fromisoformat(login["expires_at"])
    time.sleep(1.1)
    refreshed = client.post("/api/auth/refresh").json()["data"]
    assert refreshed["expires_in"] < login["expires_in"]  # lo que le queda, no 12 h nuevas
    assert abs((datetime.fromisoformat(refreshed["expires_at"]) - login_expiry).total_seconds()) <= 1


def test_expired_session_cannot_be_refreshed(client):
    _login(client)
    with SessionLocal() as db:
        db.execute(update(AuthSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        db.commit()
    expired = client.post("/api/auth/refresh")
    assert expired.status_code == 401 and expired.json()["code"] == "SESSION_INVALID"


def test_refresh_without_cookie_is_rejected(client):
    response = client.post("/api/auth/refresh")
    assert response.status_code == 401 and response.json()["code"] == "REFRESH_TOKEN_MISSING"


def test_logout_revokes_session_immediately(client):
    login = _login(client)
    assert client.get("/api/users/me", headers=_bearer(login)).status_code == 200
    out = client.post("/api/auth/logout")
    assert out.status_code == 200 and out.json()["code"] == "LOGGED_OUT"
    assert f"{COOKIE}=" in out.headers["set-cookie"] and "Max-Age=0" in out.headers["set-cookie"]
    assert client.get("/api/users/me", headers=_bearer(login)).status_code == 401


def test_sessions_list_logout_all_and_revoke_one(client, monkeypatch):
    # Varias sesiones simultáneas solo si la empresa las permite (por defecto, una).
    monkeypatch.setattr(settings, "MAX_SESSIONS_PER_USER", 10)
    a, b = _login(client), _login(client)
    page = client.get("/api/auth/sessions", headers=_bearer(b)).json()["data"]
    sessions = page["items"]
    assert page["total"] == 2 and len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    one = client.get("/api/auth/sessions", params={"size": 1, "page": 2}, headers=_bearer(b)).json()["data"]
    assert (one["total"], len(one["items"]), one["page"]) == (2, 1, 2)  # paginadas

    assert client.delete(f"/api/auth/sessions/{a.json()['data']['session_id']}", headers=_bearer(b)).status_code == 200
    assert client.get("/api/users/me", headers=_bearer(a)).status_code == 401
    assert client.delete("/api/auth/sessions/no-existe", headers=_bearer(b)).status_code == 404

    c = _login(client)
    revoked = client.post("/api/auth/logout-all", headers=_bearer(c)).json()["data"]["revoked"]
    assert revoked == 2
    assert client.get("/api/users/me", headers=_bearer(b)).status_code == 401


def test_session_limit_revokes_oldest(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_SESSIONS_PER_USER", 2)
    first = _login(client)
    second = _login(client)
    _login(client)
    assert client.get("/api/users/me", headers=_bearer(first)).status_code == 401
    assert client.get("/api/users/me", headers=_bearer(second)).status_code == 200


def test_only_one_active_session_per_user(client):
    """Por defecto un usuario tiene una sola sesión: iniciar en otro dispositivo cierra la anterior."""
    assert settings.MAX_SESSIONS_PER_USER == 1
    laptop = _login(client)
    laptop_cookie = client.cookies.get(COOKIE)
    phone = _login(client)
    assert client.get("/api/users/me", headers=_bearer(phone)).status_code == 200
    kicked = client.get("/api/users/me", headers=_bearer(laptop))
    assert kicked.status_code == 401
    assert kicked.json()["code"] == "SESSION_REPLACED"
    assert "otro dispositivo" in kicked.json()["message"]
    # Tampoco puede restaurarse con la cookie del dispositivo anterior.
    client.cookies.set(COOKIE, laptop_cookie, path="/api/auth")
    assert client.post("/api/auth/refresh").json()["code"] == "SESSION_REPLACED"


def test_deactivating_or_changing_password_revokes_employee_sessions(client, company_headers):
    emp = create_employee(client, company_headers).json()["data"]
    employee_login = _login(client, "juan@empresa.com", "Empleado123")
    client.put(f"/api/employees/{emp['id']}", json={"password": "NuevaClave123"}, headers=company_headers)
    assert client.get("/api/users/me", headers=_bearer(employee_login)).status_code == 401

    second = _login(client, "juan@empresa.com", "NuevaClave123")
    client.patch(f"/api/employees/{emp['id']}/status", json={"active": False}, headers=company_headers)
    assert client.get("/api/users/me", headers=_bearer(second)).status_code == 401


def test_database_rate_limiter_is_shared_and_atomic():
    from app.middleware.rate_limit import DatabaseRateLimiter

    limiter = DatabaseRateLimiter()
    limiter.reset()
    results = [limiter.hit("test:key", 3, 60) for _ in range(5)]
    assert results[:3] == [None, None, None]
    assert all(isinstance(r, int) and 1 <= r <= 60 for r in results[3:])
    assert limiter.hit("test:otra", 3, 60) is None


def test_logout_with_bearer_only_expires_the_active_token(client):
    """Sin cookie (p. ej. otro cliente/app): el Bearer basta para revocar la sesión al instante."""
    login = _login(client)
    client.cookies.clear()
    out = client.post("/api/auth/logout", headers=_bearer(login))
    assert out.status_code == 200 and out.json()["code"] == "LOGGED_OUT"
    me = client.get("/api/users/me", headers=_bearer(login))
    assert me.status_code == 401 and me.json()["code"] == "SESSION_REVOKED"
    # Un token inválido no rompe el logout.
    assert client.post("/api/auth/logout", headers={"Authorization": "Bearer basura"}).status_code == 200


def test_change_password(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_SESSIONS_PER_USER", 10)  # para ver que cierra las demás
    other_device, current = _login(client), _login(client)
    url = "/api/auth/change-password"

    wrong = client.post(
        url, json={"current_password": "Mala123", "new_password": "NuevaClave1"}, headers=_bearer(current)
    )
    assert wrong.status_code == 422 and wrong.json()["code"] == "CURRENT_PASSWORD_INVALID"
    weak = client.post(
        url, json={"current_password": COMPANY_PASSWORD, "new_password": "corta"}, headers=_bearer(current)
    )
    assert weak.status_code == 422 and weak.json()["errors"][0]["field"] == "new_password"
    same = client.post(
        url, json={"current_password": COMPANY_PASSWORD, "new_password": COMPANY_PASSWORD}, headers=_bearer(current)
    )
    assert same.json()["code"] == "PASSWORD_REUSED"

    changed = client.post(
        url, json={"current_password": COMPANY_PASSWORD, "new_password": "NuevaClave1"}, headers=_bearer(current)
    )
    assert changed.status_code == 200 and changed.json()["data"]["revoked_sessions"] == 1
    assert client.get("/api/users/me", headers=_bearer(current)).status_code == 200  # la sesión actual sigue
    assert client.get("/api/users/me", headers=_bearer(other_device)).status_code == 401  # las demás se cierran
    assert (
        client.post("/api/auth/login", json={"email": COMPANY_EMAIL, "password": COMPANY_PASSWORD}).status_code == 401
    )
    assert _login(client, password="NuevaClave1").status_code == 200
