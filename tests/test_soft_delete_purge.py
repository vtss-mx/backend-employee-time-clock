"""Depuración de «Eliminados» (regla 20 de la raíz): lo que una persona eliminó se borra de verdad pasados
`SOFT_DELETE_RETENTION_DAYS` (la LFT pide la asistencia hasta un año después de la baja), con lo que su borrado en
cascada se lleva; lo que otro registro aún referencia con RESTRICT espera (nunca hace fallar el lote) y una empresa con
cobranza nunca se depura."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from app.core.clock import business_today
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.soft_delete import WITH_DELETED, with_deleted
from app.models import (
    AttendanceEvent,
    Company,
    CompanyHoliday,
    Department,
    Employee,
    EmployeeWorkday,
    Shift,
    ShiftAssignment,
    User,
    Validator,
    WorkSession,
    WorkSite,
)
from app.services.maintenance_service import SOFT_DELETE_PURGES, purge_expired
from tests.billing_support import company_with_plan, pay, set_today
from tests.conftest import create_company, create_employee
from tests.test_shifts import SHIFTS, SITES, assign, create_shift, create_site
from tests.test_validators import URL as VALIDATORS
from tests.test_validators import create_validator

CAL = "/api/calendar"
TODAY = business_today()
LATER = datetime.now(UTC) + timedelta(days=settings.SOFT_DELETE_RETENTION_DAYS + 1)


def _total(model) -> int:
    with SessionLocal() as db:
        return int(db.scalar(with_deleted(select(func.count()).select_from(model))) or 0)


def _age(model, record_id: int, days: int) -> None:
    """Lo manda a «Eliminados» hace `days` días (para armar los casos de la retención y de RESTRICT)."""
    when = datetime.now(UTC) - timedelta(days=days)
    with SessionLocal() as db:
        stmt = update(model).where(model.id == record_id).values(deleted_at=when, deleted_by="prueba@empresa.com")
        db.execute(with_deleted(stmt))
        db.commit()


def _purge(now: datetime) -> dict[str, int]:
    with SessionLocal() as db:
        removed = purge_expired(db, now=now)
    return {purge.name: removed[purge.name] for purge in SOFT_DELETE_PURGES}


def test_the_trash_is_purged_only_after_its_retention(client, company_headers):
    """Cada registro eliminado sale pasado un año, con su historial (la asistencia del empleado); antes, nada."""
    employee = create_employee(client, company_headers).json()["data"]
    site = create_site(client, company_headers)
    night = create_shift(client, company_headers, name="Nocturno", sites=[site["id"]])
    current = create_shift(client, company_headers, name="Vespertino")
    assert assign(client, company_headers, employee["id"], current["id"], TODAY).status_code == 201
    change = assign(client, company_headers, employee["id"], night["id"], TODAY + timedelta(days=4)).json()["data"]
    day = TODAY + timedelta(days=20)
    holiday = client.post(
        f"{CAL}/holidays", json={"holiday_date": day.isoformat(), "name": "Feria"}, headers=company_headers
    )
    body = {"employee_id": employee["id"], "work_date": day.isoformat()}
    workday = client.post(f"{CAL}/workdays", json=body, headers=company_headers).json()["data"]
    department = client.post("/api/departments", json={"name": "Ventas"}, headers=company_headers).json()["data"]
    assert create_validator(client, company_headers).status_code == 201
    validator = client.get(VALIDATORS, headers=company_headers).json()["data"]["items"][0]
    for url in (
        f"/api/shift-assignments/{change['id']}",
        f"{CAL}/workdays/{workday['id']}",
        f"{CAL}/holidays/{holiday.json()['data']['id']}",
        f"{SHIFTS}/{night['id']}",
        f"{SITES}/{site['id']}",
        f"/api/departments/{department['id']}",
        f"{VALIDATORS}/{validator['id']}",
        f"/api/employees/{employee['id']}",
    ):
        assert client.delete(url, headers=company_headers).status_code == 200, url
    kept = (Employee, EmployeeWorkday, CompanyHoliday, Department, Shift, ShiftAssignment, WorkSite, Validator, User)
    before = {model: _total(model) for model in kept}

    assert all(count == 0 for count in _purge(datetime.now(UTC)).values())  # aún dentro de la retención
    assert {model: _total(model) for model in kept} == before

    removed = _purge(LATER)
    assert removed["cambios de turno cancelados"] == 1 and removed["días laborables eliminados"] == 1
    assert removed["festivos eliminados"] == 1 and removed["empleados eliminados"] == 1
    assert removed["turnos eliminados"] == 1 and removed["sitios eliminados"] == 1
    assert removed["departamentos eliminados"] == 1 and removed["validadores eliminados"] == 1
    assert removed["cuentas eliminadas"] == 2  # la del empleado y la del validador
    for model in (Employee, EmployeeWorkday, CompanyHoliday, Department, Validator, WorkSite):
        assert _total(model) == 0, model
    # Su asignación vigente se fue con el empleado (CASCADE); el turno que nadie eliminó se queda.
    assert _total(ShiftAssignment) == 0 and _total(Shift) == 1
    assert all(count == 0 for count in _purge(LATER).values())  # nada pendiente


def test_a_cancelled_change_and_its_restrict_references(client, company_headers):
    employee = create_employee(client, company_headers).json()["data"]
    current = create_shift(client, company_headers, name="Vespertino")
    night = create_shift(client, company_headers, name="Nocturno")
    assert assign(client, company_headers, employee["id"], current["id"], TODAY).status_code == 201
    change = assign(client, company_headers, employee["id"], night["id"], TODAY + timedelta(days=4)).json()["data"]
    assert client.delete(f"/api/shift-assignments/{change['id']}", headers=company_headers).status_code == 200
    assert client.delete(f"{SHIFTS}/{night['id']}", headers=company_headers).status_code == 200
    # Una jornada que (por error de datos) apunta al cambio cancelado lo retiene; y él retiene a su turno.
    with SessionLocal() as db:
        company_id = db.get(Employee, employee["id"]).company_id
        start = datetime.now(UTC) - timedelta(days=3)
        db.add(
            WorkSession(
                company_id=company_id,
                employee_id=employee["id"],
                assignment_id=change["id"],
                work_date=start.date(),
                shift_name="Nocturno",
                scheduled_start=start,
                scheduled_end=start + timedelta(hours=8),
                check_out_deadline=start + timedelta(hours=9),
                breaks_allowed=0,
                break_minutes_allowed=0,
                early_check_out_minutes=5,
                status="CLOSED",
                check_in_at=start,
                check_in_mode="REMOTE",
            )
        )
        db.commit()
    removed = _purge(LATER)
    assert removed["cambios de turno cancelados"] == 0 and removed["turnos eliminados"] == 0
    assert _total(ShiftAssignment) == 2 and _total(Shift) == 2


def test_restrict_references_wait_for_their_turn(client, company_headers):
    """Un sitio que aún usa un turno en «Eliminados» más reciente, un departamento que aún tiene un empleado eliminado
    hace poco y una cuenta que aún usa un empleo que se conserva esperan: nunca hacen fallar el lote."""
    site = create_site(client, company_headers)
    shift = create_shift(client, company_headers, sites=[site["id"]])
    department = client.post("/api/departments", json={"name": "Ventas"}, headers=company_headers).json()["data"]
    employee = create_employee(client, company_headers).json()["data"]
    members = f"/api/departments/{department['id']}/employees"
    assert client.post(members, json={"employee_id": employee["id"]}, headers=company_headers).status_code == 200
    _age(Shift, shift["id"], 10)
    _age(WorkSite, site["id"], 400)
    _age(Employee, employee["id"], 10)
    _age(Department, department["id"], 400)
    _age(User, employee["user_id"], 400)
    removed = _purge(datetime.now(UTC))
    assert removed["sitios eliminados"] == 0 and removed["departamentos eliminados"] == 0
    assert removed["cuentas eliminadas"] == 0
    assert _total(WorkSite) == 1 and _total(Department) == 1 and _total(User) >= 2
    # Pasado un año de lo más reciente, todo sale en una vuelta (en orden).
    removed = _purge(LATER)
    assert removed["empleados eliminados"] == 1 and removed["turnos eliminados"] == 1
    assert removed["sitios eliminados"] == 1 and removed["departamentos eliminados"] == 1
    assert removed["cuentas eliminadas"] == 1


def test_a_site_with_attendance_is_never_purged(client, company_headers):
    """Un sitio con registros no se elimina (`SITE_HAS_RECORDS`); si por datos viejos alguno quedó en «Eliminados»
    con jornadas o registros que lo nombran, la depuración lo salta (sus FK son RESTRICT) sin fallar."""
    site = create_site(client, company_headers)
    employee = create_employee(client, company_headers).json()["data"]
    shift = create_shift(client, company_headers)
    assignment = assign(client, company_headers, employee["id"], shift["id"], TODAY).json()["data"]
    _age(WorkSite, site["id"], 400)
    with SessionLocal() as db:
        company_id = db.get(Employee, employee["id"]).company_id
        start = datetime.now(UTC) - timedelta(days=2)
        session = WorkSession(
            company_id=company_id,
            employee_id=employee["id"],
            assignment_id=assignment["id"],
            work_date=start.date(),
            shift_name="Matutino",
            scheduled_start=start,
            scheduled_end=start + timedelta(hours=8),
            check_out_deadline=start + timedelta(hours=9),
            breaks_allowed=0,
            break_minutes_allowed=0,
            early_check_out_minutes=5,
            status="CLOSED",
            check_in_at=start,
            check_in_mode="ON_SITE",
            check_in_site_id=site["id"],
        )
        db.add(session)
        db.flush()
        db.add(
            AttendanceEvent(
                company_id=company_id,
                employee_id=employee["id"],
                session_id=session.id,
                action="CHECK_IN",
                mode="ON_SITE",
                site_id=site["id"],
                occurred_at=start,
            )
        )
        db.commit()
    assert _purge(LATER)["sitios eliminados"] == 0 and _total(WorkSite) == 1


def test_companies_wait_for_their_people_and_never_lose_billing(client, admin_headers, monkeypatch):
    set_today(monkeypatch, TODAY)
    # Una empresa en «Eliminados» con cobranza (no debería existir: eliminarla exige no tener movimientos) nunca se
    # depura: el historial fiscal se conserva aunque ya no le quede ninguna cuenta.
    billed = company_with_plan(client, admin_headers)
    assert pay(client, admin_headers, billed, "50", TODAY).status_code == 201
    _age(Company, billed, 400)
    with SessionLocal() as db:
        for user_id in db.scalars(select(User.id).where(User.company_id == billed)):
            _age(User, user_id, 400)
    # Otra, eliminada con sus cuentas: sale junto con ellas pasado un año.
    clean = create_company(client, admin_headers, rfc="CLN120315AB1", admin_email="c@c.com").json()["data"]["id"]
    assert client.delete(f"/api/admin/companies/{clean}", headers=admin_headers).status_code == 200
    removed = _purge(datetime.now(UTC))
    assert removed["empresas eliminadas"] == 0 and removed["cuentas eliminadas"] == 1  # la cuenta de la de cobranza
    removed = _purge(LATER)
    assert removed["cuentas eliminadas"] == 1 and removed["empresas eliminadas"] == 1
    with SessionLocal() as db:
        assert db.get(Company, billed, execution_options=WITH_DELETED) is not None
        assert db.get(Company, clean, execution_options=WITH_DELETED) is None
