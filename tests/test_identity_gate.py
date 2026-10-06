"""Mientras su identidad no esté aprobada, el empleado solo usa su identidad y su cuenta.

Decisión del dueño del producto: un empleado sin registro facial aprobado (sin registrar, en validación
o rechazado) solo tiene el módulo de identidad (registrar su rostro / verlo en validación) y su cuenta
(perfil). El menú lo dicen sus pantallas y cada endpoint de las demás responde 403 FACE_NOT_APPROVED.
"""

import inspect
from types import SimpleNamespace

import pytest

from app.core.exceptions import ConflictError
from app.dependencies import require_screen
from app.models import Screen, UserRole
from app.services.catalog_service import get_catalogs
from app.services.navigation_service import IDENTITY_SCREENS
from tests.conftest import create_employee, login, submit_enrollment
from tests.test_authorization import _named, _routes, _url

ALLOWED_MODULES = {"IDENTITY", "ACCOUNT"}


def _identity_only(calls: list) -> bool:
    """La ruta solo sirve a pantallas del empleado que exigen la identidad aprobada."""
    granted: set[str] = set()
    for guard in _named(calls, "require_screen."):
        screens = inspect.getclosurevars(guard).nonlocals["screens"]
        granted |= {s for s in screens if get_catalogs().grants(UserRole.EMPLOYEE.value, (s,))}
    return bool(granted) and granted <= IDENTITY_SCREENS


@pytest.fixture(params=["NOT_ENROLLED", "PENDING_REVIEW"])
def unapproved(request, client, company_headers) -> tuple[str, dict[str, str]]:
    assert create_employee(client, company_headers).status_code == 201
    headers = login(client, "juan@empresa.com", "Empleado123")
    if request.param == "PENDING_REVIEW":
        assert submit_enrollment(client, headers).status_code == 201
    return request.param, headers


def test_the_menu_only_has_identity_and_account(client, unapproved):
    status, headers = unapproved
    me = client.get("/api/users/me", headers=headers).json()["data"]
    codes = [screen["code"] for screen in me["screens"]]
    home = Screen.EMPLOYEE_ENROLL if status == "NOT_ENROLLED" else Screen.EMPLOYEE_PENDING
    assert codes == [home, Screen.PROFILE]
    assert {module["code"] for module in me["modules"]} <= ALLOWED_MODULES
    assert not IDENTITY_SCREENS & set(codes)


def test_every_api_of_the_other_screens_is_closed(client, unapproved):
    status, headers = unapproved
    reason = "validación" if status == "PENDING_REVIEW" else "registrar tu rostro"
    checked, leaks = 0, []
    for method, path, calls in _routes():
        if not _identity_only(calls):
            continue
        checked += 1
        body = client.request(method, _url(path), headers=headers).json()
        if body.get("statusCode") != 403 or body.get("code") != "FACE_NOT_APPROVED" or reason not in body["message"]:
            leaks.append(f"{method} {path} → {body.get('statusCode')} {body.get('code')}")
    assert checked >= 10  # asistencia, días libres, solicitudes, QR y verificación
    assert leaks == [], "APIs abiertas sin identidad aprobada:\n" + "\n".join(leaks)


def test_an_employee_of_several_companies_must_choose_first():
    """Sin empresa elegida, la API pide elegirla (409) antes de revisar su identidad."""
    person = SimpleNamespace(role=UserRole.EMPLOYEE, current_company=None, employee=None)
    with pytest.raises(ConflictError) as error:
        require_screen(Screen.EMPLOYEE_ATTENDANCE)(person)
    assert error.value.code == "COMPANY_SELECTION_REQUIRED"
