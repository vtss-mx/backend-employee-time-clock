"""Carreras entre peticiones simultáneas: dos altas o dos lecturas del mismo dato al mismo tiempo.

Las verificaciones previas ("¿ya existe?") no bastan cuando otra petición guarda lo mismo entre la
verificación y la escritura: ahí deciden las restricciones únicas y las sentencias atómicas de la
BD. Cada prueba hace que OTRA sesión de BD (otra petición) se adelante en el instante exacto y
confirme; la petición que pierde responde su código estable, sin escrituras a medias.
"""

import logging
from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Company, User, UserRole
from app.models.company import TaxId
from app.repositories.company_repository import CompanyRepository
from app.repositories.qr_repository import EmployeeQrRepository
from app.repositories.user_repository import UserRepository
from app.services.bootstrap import ensure_first_admin
from app.services.people_service import PeopleService
from app.services.qr_service import QrService
from tests.conftest import COMPANY_EMAIL, create_company, create_employee, qr_content

Competitor = Callable[..., Any]


def _in_another_session(competitor: Competitor, *args: Any, **kwargs: Any) -> None:
    """Lo que hace la OTRA petición: su propia sesión de BD y su propia confirmación."""
    with SessionLocal() as other:
        competitor(other, *args, **kwargs)
        other.commit()


def _wins_before(monkeypatch, owner: type, name: str, competitor: Competitor) -> None:
    """Ambas pasaron las verificaciones; la otra ejecuta su escritura justo antes que esta."""
    original = getattr(owner, name)

    def raced(self, *args, **kwargs):
        monkeypatch.setattr(owner, name, original)  # la otra petición se adelanta una sola vez
        _in_another_session(competitor, *args, **kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(owner, name, raced)


def _wins_after_check(monkeypatch, owner: type, name: str, competitor: Competitor) -> None:
    """ "Consultar y luego actuar": la otra guarda el dato que choca justo DESPUÉS de que esta lo
    verificó libre."""
    original = getattr(owner, name)

    def raced(self, *args, **kwargs):
        monkeypatch.setattr(owner, name, original)
        result = original(self, *args, **kwargs)
        _in_another_session(competitor, *args, **kwargs)
        return result

    monkeypatch.setattr(owner, name, raced)


def _same_statement(repository: type, name: str) -> Competitor:
    """La otra petición ejecuta exactamente la misma sentencia (p. ej. un doble envío)."""
    return lambda other, *args, **kwargs: getattr(repository(other), name)(*args, **kwargs)


def _twin_company(other, tax: TaxId | None, *_: Any, **__: Any) -> None:
    """Otra empresa con el mismo identificador fiscal (país, tipo y número)."""
    twin = Company(name="Empresa gemela", active=True)
    twin.set_tax(tax)
    other.add(twin)


def _account_with(other, email: str, *_: Any, **__: Any) -> None:
    UserRepository(other).create(email=email, password_hash="x", role=UserRole.EMPLOYEE)


def _count_users(email: str) -> int:
    with SessionLocal() as db:
        return db.query(User).filter_by(email=email).count()


# ---------------------------------------------------------------- empresas


def test_two_simultaneous_registrations_of_the_same_company(client, admin_headers, monkeypatch):
    _wins_before(monkeypatch, CompanyRepository, "add", lambda other, company: _twin_company(other, company.tax))
    lost = create_company(client, admin_headers)
    assert lost.status_code == 409 and lost.json()["code"] == "DUPLICATE"
    assert _count_users("admin@panificadora.com") == 0  # ni la empresa ni su administrador a medias


def test_two_companies_claiming_the_same_tax_id_at_once(client, admin_headers, monkeypatch):
    """El índice único parcial (país, tipo, número) decide: la que pierde responde 409 en `tax_id`."""
    company = create_company(client, admin_headers).json()["data"]
    _wins_after_check(monkeypatch, CompanyRepository, "tax_id_exists", _twin_company)
    body = {"tax_country": "US", "tax_id_type": "US_EIN", "tax_id": "12-3456789"}
    lost = client.put(f"/api/admin/companies/{company['id']}", json=body, headers=admin_headers)
    assert lost.status_code == 409 and lost.json()["code"] == "COMPANY_TAX_ID_TAKEN"
    assert lost.json()["errors"][0]["field"] == "tax_id"
    detail = client.get(f"/api/admin/companies/{company['id']}", headers=admin_headers).json()["data"]
    assert (detail["tax_id_type"], detail["tax_id"]) == ("MX_RFC", "PNO120315AB1")  # conserva su RFC


# ---------------------------------------------------------------- empleados


def test_two_simultaneous_registrations_of_the_same_person(client, company_headers, monkeypatch):
    _wins_before(monkeypatch, UserRepository, "create", _same_statement(UserRepository, "create"))
    lost = create_employee(client, company_headers)
    assert lost.status_code == 409 and lost.json()["code"] == "DUPLICATE"
    assert client.get("/api/employees", headers=company_headers).json()["data"]["total"] == 0


def test_an_email_taken_while_editing_an_employee(client, company_headers, monkeypatch):
    employee = create_employee(client, company_headers).json()["data"]
    _wins_after_check(monkeypatch, PeopleService, "check_email", _account_with)
    lost = client.put(f"/api/employees/{employee['id']}", json={"email": "nuevo@empresa.com"}, headers=company_headers)
    assert lost.status_code == 409 and lost.json()["code"] == "DUPLICATE"
    current = client.get(f"/api/employees/{employee['id']}", headers=company_headers).json()["data"]
    assert current["email"] == "juan@empresa.com"


# ---------------------------------------------------------------- usuarios iniciales


def test_several_api_processes_create_the_first_admin_only_once(monkeypatch, caplog):
    """Al arrancar varios procesos a la vez, solo uno crea el ADMIN inicial; los demás no fallan."""
    monkeypatch.setattr(settings, "FIRST_ADMIN_EMAIL", "primero@plataforma.com")
    monkeypatch.setattr(settings, "FIRST_ADMIN_PASSWORD", "Primero12345")
    _wins_before(monkeypatch, UserRepository, "create", _same_statement(UserRepository, "create"))
    with SessionLocal() as db, caplog.at_level(logging.INFO, logger="app.services.bootstrap"):
        ensure_first_admin(db)
    assert _count_users("primero@plataforma.com") == 1
    # El proceso que perdió no lo anuncia como creado ni lo reporta como error.
    assert not [r for r in caplog.records if r.name == "app.services.bootstrap"]


# ---------------------------------------------------------------- QR de un solo uso


def _qr_setup(client, company_headers) -> tuple[str, int, int]:
    """(contenido del QR, empresa, quién lo lee) de un empleado activo."""
    employee = create_employee(client, company_headers).json()["data"]
    with SessionLocal() as db:
        reader = UserRepository(db).get_by_email(COMPANY_EMAIL)
        assert reader is not None and reader.company_id is not None
        return qr_content(employee["id"]), reader.company_id, reader.id


def test_two_readers_scanning_the_same_qr_at_once(client, company_headers, monkeypatch):
    content, company_id, reader = _qr_setup(client, company_headers)
    _wins_before(monkeypatch, EmployeeQrRepository, "mark_used", _same_statement(EmployeeQrRepository, "mark_used"))
    with SessionLocal() as db:
        lost = QrService(db).use(content, company_id=company_id, actor_id=reader)
    assert lost.employee is None and lost.reason == "ALREADY_USED"


def test_the_face_step_of_a_held_qr_completes_only_once(client, company_headers, monkeypatch):
    content, company_id, reader = _qr_setup(client, company_headers)
    with SessionLocal() as db:
        assert QrService(db).use(content, company_id=company_id, actor_id=reader, hold=True).employee is not None
        db.commit()
    competitor = _same_statement(EmployeeQrRepository, "mark_completed")
    _wins_before(monkeypatch, EmployeeQrRepository, "mark_completed", competitor)
    with SessionLocal() as db:
        lost = QrService(db).complete_hold(content, company_id=company_id, actor_id=reader)
    assert lost.employee is None and lost.reason == "ALREADY_USED"
