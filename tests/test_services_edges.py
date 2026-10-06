"""Reglas de negocio en sus casos límite: datos que ya tiene otro, configuraciones incompletas o mal
hechas en la BD, cachés acotadas, catálogos que no cargan y entradas mal formadas."""

import logging
import threading

import pytest
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.error_events import ErrorEvent
from app.core.exceptions import PermissionDeniedError
from app.models import Employee, FaceStatus, RememberedAccount, RoleScreen, Screen, User, UserRole
from app.repositories.user_repository import UserRepository
from app.services import catalog_service, policy_service
from app.services.bootstrap import ensure_first_company
from app.services.catalog_service import clear_catalog_cache, get_catalogs
from app.services.device_service import issue_nonce, signature_is_valid
from app.services.employee_access import active_employee, approved_employee
from app.services.error_reporter import ErrorReporter
from app.services.policy_service import PolicyService
from app.services.qr_service import QR_PREFIX, QrService
from tests.conftest import (
    COMPANY_EMAIL,
    COMPANY_PASSWORD,
    create_company,
    create_employee,
    device_proof,
    login,
)
from tests.test_validators import PASSWORD as VALIDATOR_PASSWORD
from tests.test_validators import create_validator

WAIT_SECONDS = 5


def _company_of(db, email: str) -> int:
    account = UserRepository(db).get_by_email(email)
    assert account is not None and account.company_id is not None
    return account.company_id


def _validate(client, headers, field: str, value: str):
    return client.get("/api/validation", params={"field": field, "value": value}, headers=headers)


# ---------------------------------------------------------------- catálogos


def test_a_message_keeps_its_marker_when_the_data_is_missing():
    """Si el motor no envía el dato de un {marcador}, el mensaje sale igual (sin romper la respuesta)."""
    catalogs = get_catalogs()
    assert catalogs.face_error_message("IMAGE_TOO_SMALL") == "La imagen debe medir al menos {min_dimension}px por lado"
    assert "640px" in catalogs.face_error_message("IMAGE_TOO_SMALL", {"min_dimension": 640})


def test_while_one_request_reloads_the_catalogs_the_others_keep_the_previous_ones(monkeypatch):
    cache = catalog_service._CatalogCache()
    previous = cache.get()
    monkeypatch.setattr(settings, "CATALOG_CACHE_SECONDS", 0)  # vencidos: la siguiente lectura recarga
    entered, release = threading.Event(), threading.Event()
    real_session = catalog_service.SessionLocal

    def slow_database():
        entered.set()
        release.wait(WAIT_SECONDS)
        return real_session()

    monkeypatch.setattr(catalog_service, "SessionLocal", slow_database)
    reloaded: list = []
    reloader = threading.Thread(target=lambda: reloaded.append(cache.get()))
    reloader.start()
    try:
        assert entered.wait(WAIT_SECONDS)
        assert cache.get() is previous  # no espera a la recarga ni dispara otra
    finally:
        release.set()
        reloader.join(WAIT_SECONDS)
    assert reloaded and reloaded[0] is not previous and reloaded[0]["es-MX"].name("roles", "ADMIN")


def test_without_previous_catalogs_a_database_failure_is_reported_and_retried(monkeypatch):
    """Sin catálogos anteriores no hay con qué degradar: la falla sale con su error de BD (el
    manejador global responde DATABASE_UNAVAILABLE) y la siguiente petición vuelve a intentar."""
    cache = catalog_service._CatalogCache()
    real_session = catalog_service.SessionLocal

    def database_down():
        raise OperationalError("SELECT", {}, Exception("BD caída"))

    monkeypatch.setattr(catalog_service, "SessionLocal", database_down)
    with pytest.raises(OperationalError):
        cache.get()
    monkeypatch.setattr(catalog_service, "SessionLocal", real_session)
    assert cache.get()["en-US"].name("roles", "COMPANY")  # no quedó atorada "recargando"


# ---------------------------------------------------------------- empresas y usuarios iniciales


def test_a_company_cannot_take_the_rfc_of_another(client, admin_headers):
    create_company(client, admin_headers)
    other = create_company(client, admin_headers, rfc="ACM010101AB2", admin_email="admin@acme.com").json()["data"]
    taken = client.put(f"/api/admin/companies/{other['id']}", json={"rfc": "PNO120315AB1"}, headers=admin_headers)
    assert taken.status_code == 409 and taken.json()["code"] == "COMPANY_TAX_ID_TAKEN"
    assert taken.json()["errors"][0]["field"] == "tax_id"


def test_the_first_company_admin_is_created_from_the_environment(monkeypatch, caplog):
    monkeypatch.setattr(settings, "FIRST_COMPANY_EMAIL", "inicial@empresa.com")
    monkeypatch.setattr(settings, "FIRST_COMPANY_PASSWORD", "Inicial12345")
    with SessionLocal() as db, caplog.at_level(logging.INFO, logger="app.services.bootstrap"):
        ensure_first_company(db)
        created = UserRepository(db).get_by_email("inicial@empresa.com")
        assert created is not None and created.role == UserRole.COMPANY and created.company_id is not None
    assert any("inicial@empresa.com" in r.getMessage() for r in caplog.records)


def test_an_invalid_first_user_is_reported_and_the_api_still_starts(monkeypatch, caplog):
    monkeypatch.setattr(settings, "FIRST_COMPANY_EMAIL", "debil@empresa.com")
    monkeypatch.setattr(settings, "FIRST_COMPANY_PASSWORD", "corta")
    with SessionLocal() as db, caplog.at_level(logging.ERROR, logger="app.services.bootstrap"):
        ensure_first_company(db)  # no lanza: el arranque sigue
        assert UserRepository(db).get_by_email("debil@empresa.com") is None
    assert any(r.levelno == logging.ERROR and "COMPANY" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- dispositivos de validadores


def test_a_signature_that_is_not_base64_is_invalid():
    nonce = issue_nonce(7)
    assert not signature_is_valid(device_proof(nonce)["public_key"], nonce, "¡no-es-base64!" * 4)


def test_a_device_without_a_name_is_named_by_its_kind(client, company_headers):
    """El navegador no siempre dice su nombre: la empresa ve al menos qué tipo de equipo es."""
    create_validator(client, company_headers)
    body = {"email": "recepcion@empresa.com", "password": VALIDATOR_PASSWORD}
    nonce = client.post("/api/auth/login", json=body).json()["errors"][0]["details"]["nonce"]
    pending = client.post("/api/auth/login", json={**body, "device": device_proof(nonce, name="   ")})
    assert pending.status_code == 403 and pending.json()["code"] == "DEVICE_PENDING_APPROVAL"
    assert "Teléfono" in pending.json()["message"]  # el cliente de pruebas se presenta como iPhone


# ---------------------------------------------------------------- empleados y personas


def test_a_company_changes_the_email_of_its_own_employee(client, company_headers):
    employee = create_employee(client, company_headers).json()["data"]
    changed = client.put(
        f"/api/employees/{employee['id']}", json={"email": "Juan.Nuevo@Empresa.com"}, headers=company_headers
    )
    assert changed.status_code == 200 and changed.json()["data"]["email"] == "juan.nuevo@empresa.com"
    assert login(client, "juan.nuevo@empresa.com", "Empleado123")


def test_an_employee_cannot_take_the_email_of_another_person(client, company_headers):
    create_employee(client, company_headers)
    other = create_employee(client, company_headers, number="EMP-002", email="ana@empresa.com").json()["data"]
    taken = client.put(f"/api/employees/{other['id']}", json={"email": "juan@empresa.com"}, headers=company_headers)
    assert taken.status_code == 409 and taken.json()["code"] == "EMAIL_TAKEN"
    assert taken.json()["errors"][0]["field"] == "email"


def test_an_account_without_a_phone_cannot_be_linked_to_another_company(client, admin_headers, company_headers):
    """Para vincular a una persona de otra empresa su teléfono debe coincidir; si su cuenta aún no
    tiene uno (cuentas anteriores a ese dato), su empresa actual debe capturarlo primero."""
    create_employee(client, company_headers)
    with SessionLocal() as db:
        account = UserRepository(db).get_by_email("juan@empresa.com")
        assert account is not None
        account.phone = None
        db.commit()
    create_company(client, admin_headers)
    bakery = login(client, "admin@panificadora.com", "Empresa1234")
    linked = create_employee(client, bakery, number="PAN-1")
    assert linked.status_code == 409 and linked.json()["code"] == "ACCOUNT_PHONE_MISSING"
    assert linked.json()["errors"][0]["field"] == "phone"


def _person(role: UserRole, *employees: Employee, active: bool = True) -> User:
    user = User(email="persona@empresa.com", password_hash="x", role=role, active=active)
    user.employees = list(employees)
    return user


def test_employee_functions_require_an_active_job():
    """Reglas puras de acceso de las funciones del empleado (identificarse, su QR, su rostro)."""
    job = Employee(company_id=1, active=True, face_status=FaceStatus.APPROVED)
    assert approved_employee(_person(UserRole.EMPLOYEE, job)) is job

    for user, code in (
        (_person(UserRole.COMPANY), "FORBIDDEN"),  # sin empleo: no es empleado
        (_person(UserRole.EMPLOYEE, Employee(company_id=1, active=False)), "USER_INACTIVE"),
        (_person(UserRole.EMPLOYEE, job, active=False), "USER_INACTIVE"),
    ):
        with pytest.raises(PermissionDeniedError) as denied:
            active_employee(user)
        assert denied.value.code == code

    pending = Employee(company_id=1, active=True, face_status=FaceStatus.PENDING_REVIEW)
    with pytest.raises(PermissionDeniedError) as denied:
        approved_employee(_person(UserRole.EMPLOYEE, pending))
    assert denied.value.code == "FACE_NOT_APPROVED" and "validación" in denied.value.message


# ---------------------------------------------------------------- validación en vivo


def test_a_misconfigured_screen_never_lets_another_role_validate_company_data(client, admin_headers):
    """Si la BD diera por error al ADMIN las pantallas de empleados y departamentos, el código sigue
    sin dejarlo validar datos de una empresa (no tiene una)."""
    with SessionLocal() as db:
        db.add_all(
            RoleScreen(role_code=UserRole.ADMIN, screen_code=screen)
            for screen in (Screen.COMPANY_EMPLOYEES, Screen.COMPANY_DEPARTMENTS)
        )
        db.commit()
    clear_catalog_cache()
    for field, value in (("email", "nuevo@empresa.com"), ("department_name", "Ventas")):
        denied = _validate(client, admin_headers, field, value)
        assert denied.status_code == 403 and denied.json()["code"] == "COMPANY_REQUIRED", field


def test_an_empty_company_phone_is_reported_as_empty(client, admin_headers):
    assert _validate(client, admin_headers, "company_phone", "   ").json()["data"]["code"] == "EMPTY"


# ---------------------------------------------------------------- política (caché acotada)


def test_the_policy_cache_forgets_the_least_used_company(client, admin_headers, monkeypatch):
    """La caché por proceso no crece sin límite: al pasarse del tope sale la menos usada."""
    other = create_company(client, admin_headers).json()["data"]["id"]
    monkeypatch.setattr(settings, "POLICY_CACHE_COMPANIES", 1)
    with SessionLocal() as db:
        mine = _company_of(db, COMPANY_EMAIL)
        first = PolicyService(db, mine).current()
        PolicyService(db, other).current()
        assert list(policy_service._cache) == [other]
        assert PolicyService(db, mine).current() == first  # se vuelve a leer de la BD, igual
        assert list(policy_service._cache) == [mine]


# ---------------------------------------------------------------- QR


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (QR_PREFIX, "INVALID_FORMAT"),  # sin token
        (QR_PREFIX + "x" * 129, "INVALID_FORMAT"),  # más largo que cualquier token emitido
        (QR_PREFIX + "token-que-nadie-emitio", "NOT_FOUND"),
        ("https://otro-sistema.com/qr", "INVALID_FORMAT"),
    ],
)
def test_an_unreadable_or_unknown_qr_is_rejected_without_consuming_anything(client, content, reason):
    with SessionLocal() as db:
        service = QrService(db)
        assert service.use(content, company_id=1, actor_id=1).reason == reason
        assert service.complete_hold(content, company_id=1, actor_id=1).reason == reason  # QR + rostro


# ---------------------------------------------------------------- cuenta recordada


def test_forgetting_with_a_foreign_cookie_keeps_the_remembered_account(client):
    """Una cookie alterada (o de otro dispositivo) no borra la cuenta recordada de nadie."""
    response = client.post(
        "/api/auth/login", json={"email": COMPANY_EMAIL, "password": COMPANY_PASSWORD, "remember": True}
    )
    assert response.status_code == 200
    client.cookies.set(settings.REMEMBER_COOKIE_NAME, "cuenta-inventada.secreto", path="/api/auth")
    assert client.delete("/api/auth/remembered").status_code == 200
    with SessionLocal() as db:
        assert db.query(RememberedAccount).count() == 1


# ---------------------------------------------------------------- errores del sistema


def test_error_tracking_details(client, admin_headers):
    """Un error de segundo plano (sin usuario) se ve sin "quién lo provocó"; volver a marcar el mismo
    estado no se registra como un cambio; el filtro por severidad separa lo crítico."""
    reporter = ErrorReporter(capacity=10)
    reporter.report(ErrorEvent(source="LOG", severity="CRITICAL", code="HILO_CAIDO", message="falló"))
    assert reporter.flush() == 1
    url = "/api/admin/errors"
    report = client.get(url, params={"severity": "CRITICAL"}, headers=admin_headers).json()["data"]["items"]
    assert [r["code"] for r in report] == ["HILO_CAIDO"]
    assert client.get(url, params={"severity": "WARNING"}, headers=admin_headers).json()["data"]["total"] == 0

    occurrences = client.get(f"{url}/{report[0]['id']}/occurrences", headers=admin_headers).json()["data"]
    assert occurrences["items"][0]["user_label"] is None
    same = client.patch(f"{url}/{report[0]['id']}/status", json={"status": "PENDING"}, headers=admin_headers)
    assert same.status_code == 200 and same.json()["data"]["status_changed_by"] is None
