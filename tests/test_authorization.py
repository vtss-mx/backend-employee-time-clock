"""Autorización estricta: cada usuario solo puede usar las APIs de su rol.

1. Inventario: TODA ruta de la API declara su autorización (pantalla + rol, o llave de integración),
   salvo una lista explícita de rutas públicas y de rutas de cualquier sesión. Una ruta nueva sin
   autorización hace fallar esta prueba (no se puede olvidar).
2. Matriz: cada ruta se llama con cada rol que NO tiene permiso y sin sesión: siempre 403 / 401,
   nunca un 200, 404, 422 ni 500. Así un COMPANY no puede usar nada del ADMIN, ni al revés, ni un
   EMPLOYEE lo de un VALIDATOR, etc. (sin efectos: solo se prueban los roles denegados).
"""

import inspect
import re

import pytest
from fastapi.routing import APIRoute

from app.main import API_ROUTERS
from app.models import UserRole
from tests.conftest import approved_employee
from tests.test_validators import validator_headers

PREFIX = "/api"

#: Sin sesión: salud, iniciar y renovar sesión, cuenta recordada, llaves públicas de los JWT, el
#: reporte de fallas de la app web (una pantalla puede romperse en el login: no puede exigir sesión;
#: va limitado por IP, con tope de tamaño, y si trae un token válido anota quién) y el rendimiento que
#: mide el navegador (también en el login; sin sesión solo cuenta esa pantalla, límite estricto por IP). La tableta del
#: kiosco de un sitio (antifraude 2b): no tiene sesión; la autentica la llave de su dispositivo (firma por petición) o,
#: al vincularse, el código de un solo uso que dio la empresa (limitada por IP; una sesión no le da nada).
PUBLIC = {
    ("POST", "/kiosk/pair"),
    ("POST", "/kiosk/code"),
    ("POST", "/client-errors"),
    ("POST", "/telemetry/web"),
    ("GET", "/health"),
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
    ("POST", "/auth/login"),
    ("POST", "/auth/refresh"),
    ("GET", "/auth/session"),
    ("POST", "/auth/logout"),
    ("GET", "/auth/remembered"),
    ("DELETE", "/auth/remembered"),
    ("GET", "/auth/jwks"),
}
#: Cualquier sesión (todos los roles): su propia cuenta, sus sesiones, catálogos y validación en vivo
#: (cada campo exige su pantalla dentro del servicio). La política se lee con una empresa de sesión.
ANY_SESSION = {
    ("POST", "/auth/change-password"),
    ("POST", "/auth/logout-all"),
    ("GET", "/auth/sessions"),
    ("DELETE", "/auth/sessions/{session_id}"),
    ("GET", "/users/me"),
    ("PATCH", "/users/me/preferences"),
    ("GET", "/catalogs"),
    ("GET", "/validation"),
    ("GET", "/settings/verification"),
}


def _calls(dependant) -> list:
    found = []
    for dependency in dependant.dependencies:
        found.append(dependency.call)
        found.extend(_calls(dependency))
    return found


def _routes():
    for router in API_ROUTERS:
        for route in router.routes:
            if isinstance(route, APIRoute):
                for method in sorted(route.methods - {"HEAD"}):
                    yield method, route.path, _calls(route.dependant)


def _named(calls: list, name: str) -> list:
    return [c for c in calls if getattr(c, "__qualname__", "").startswith(name)]


def _allowed_roles(calls: list) -> set[UserRole] | None:
    """Roles que admite la ruta según sus guardias de rol (intersección); None = sin guardia de rol."""
    allowed: set[UserRole] | None = None
    for guard in _named(calls, "require_roles."):
        roles = set(inspect.getclosurevars(guard).nonlocals["roles"])
        allowed = roles if allowed is None else allowed & roles
    return allowed


def test_every_route_declares_its_authorization():
    unguarded = []
    for method, path, calls in _routes():
        key = (method, path)
        if key in PUBLIC:
            continue
        has_session = bool(_named(calls, "get_current_user") or _named(calls, "optional_token_payload"))
        if key in ANY_SESSION:
            assert has_session, f"{method} {path} debe exigir sesión"
            continue
        api_key = bool(_named(calls, "get_api_client"))
        screen_and_role = bool(_named(calls, "require_screen.")) and _allowed_roles(calls) is not None
        if not (api_key or screen_and_role):
            unguarded.append(f"{method} {path}")
    assert unguarded == [], f"Rutas sin pantalla + rol ni llave de integración: {unguarded}"


@pytest.fixture
def tokens(client, admin_headers, company_headers) -> dict[UserRole, dict[str, str]]:
    return {
        UserRole.ADMIN: admin_headers,
        UserRole.COMPANY: company_headers,
        UserRole.VALIDATOR: validator_headers(client, company_headers, mode="QR_OR_FACE"),
        UserRole.EMPLOYEE: approved_employee(client, company_headers),
    }


def _url(path: str) -> str:
    return PREFIX + re.sub(r"\{[^}]+\}", "1", path)


def test_each_role_only_reaches_its_own_apis(client, tokens):
    leaks = []
    for method, path, calls in _routes():
        if (method, path) in PUBLIC or (method, path) in ANY_SESSION:
            continue
        url = _url(path)
        anonymous = client.request(method, url)
        if anonymous.status_code != 401:
            leaks.append(f"sin sesión {method} {path} → {anonymous.status_code}")
        allowed = _allowed_roles(calls)
        denied = set(UserRole) if allowed is None else set(UserRole) - allowed
        for role in sorted(denied):
            response = client.request(method, url, headers=tokens[role])
            expected = 401 if _named(calls, "get_api_client") else 403  # la integración solo acepta llave
            body = response.json()
            if response.status_code != expected or body.get("success") is not False:
                leaks.append(f"{role.value} {method} {path} → {response.status_code} {body.get('code')}")
    assert leaks == [], "Accesos indebidos:\n" + "\n".join(leaks)


def test_integration_api_never_accepts_a_user_session(client, tokens):
    for method, path, calls in _routes():
        if not _named(calls, "get_api_client"):
            continue
        for headers in tokens.values():
            response = client.request(method, _url(path), headers=headers)
            assert response.status_code == 401 and response.json()["code"] in ("API_KEY_REQUIRED", "API_KEY_INVALID")
