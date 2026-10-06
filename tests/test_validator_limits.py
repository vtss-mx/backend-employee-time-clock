"""Validadores por empresa (decisión del dueño del producto, migración 0067): el ADMIN decide cuántos validadores
ACTIVOS puede tener cada empresa; con 0 el módulo está apagado (sin pantalla y 403 en sus APIs); el límite nunca baja
de los activos; dar de alta o activar más allá del límite responde 409 sin carreras; y cada validador activo cuenta
como un empleado en el cobro por empleado activo, día por día."""

from datetime import date

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import Charge, HeadcountDay, User, ValidatorStatusEvent
from app.repositories.billing_repository import BillingRepository
from app.schemas.validator import ValidatorCreate
from app.services import billing_jobs
from app.services.validator_service import ValidatorService
from tests.billing_support import PLAN, at, billing_url, hire, run_jobs, set_today
from tests.conftest import COMPANY_EMAIL, create_company, create_employee, login
from tests.test_api_keys import call, new_key
from tests.test_races import _wins_before
from tests.test_validators import ADDRESS, PASSWORD, URL, create_validator

ADMIN_COMPANIES = "/api/admin/companies"
NOV, DEC = date(2026, 11, 1), date(2026, 12, 1)
OWNER = ("admin@panificadora.com", "Empresa1234")
ENGLISH = {"Accept-Language": "en-US"}


def _company_id() -> int:
    with SessionLocal() as db:
        company_id = db.scalar(select(User.company_id).where(User.email == COMPANY_EMAIL))
    assert company_id is not None
    return company_id


def _set_limit(client, admin_headers, company_id: int, limit: int):
    return client.put(f"{ADMIN_COMPANIES}/{company_id}", json={"max_validators": limit}, headers=admin_headers)


def _screens(client, headers) -> list[str]:
    return [s["code"] for s in client.get("/api/users/me", headers=headers).json()["data"]["screens"]]


def _events(company_id: int) -> list[tuple[int, bool]]:
    with SessionLocal() as db:
        rows = db.execute(
            select(ValidatorStatusEvent.validator_id, ValidatorStatusEvent.active)
            .where(ValidatorStatusEvent.company_id == company_id)
            .order_by(ValidatorStatusEvent.id)
        )
        return [(int(v), bool(a)) for v, a in rows]


def _validator(i: int) -> dict:
    return {"name": f"Acceso {i}", "email": f"acceso{i}@empresa.com", "password": PASSWORD, "address": ADDRESS}


# ---------------------------------------------------------------- módulo apagado (límite 0)


def test_a_new_company_starts_without_the_validators_module(client, admin_headers):
    created = create_company(client, admin_headers).json()["data"]
    assert (created["max_validators"], created["active_validators"]) == (0, 0)
    owner = login(client, *OWNER)
    assert "COMPANY_VALIDATORS" not in _screens(client, owner)
    # Toda API que administra validadores responde 403 con su código (también la validación en vivo del correo).
    calls = [
        ("GET", URL, None),
        ("GET", f"{URL}/1", None),
        ("POST", URL, _validator(1)),
        ("PUT", f"{URL}/1", {"name": "Otro"}),
        ("PATCH", f"{URL}/1/status", {"active": True}),
        ("PUT", f"{URL}/1/password", {"password": "Nueva12345"}),
        ("DELETE", f"{URL}/1", None),
        ("GET", f"{URL}/1/devices", None),
        ("PATCH", f"{URL}/1/devices/1/status", {"status": "APPROVED"}),
    ]
    for method, url, body in calls:
        response = client.request(method, url, json=body, headers=owner)
        assert (response.status_code, response.json()["code"]) == (403, "VALIDATORS_DISABLED"), (method, url)
    live = client.get("/api/validation", params={"field": "validator_email", "value": "a@b.com"}, headers=owner)
    assert (live.status_code, live.json()["code"]) == (403, "VALIDATORS_DISABLED")
    assert "administrador de la plataforma" in live.json()["message"]
    english = client.get(URL, headers={**owner, **ENGLISH}).json()["message"]
    assert english == "Your company doesn't have the validators module. Ask the platform administrator for it."


def test_the_admin_grants_seats_and_the_company_uses_them(client, admin_headers, company_headers):
    company_id = _company_id()
    granted = _set_limit(client, admin_headers, company_id, 2)
    assert granted.status_code == 200 and granted.json()["data"]["max_validators"] == 2
    assert "COMPANY_VALIDATORS" in _screens(client, company_headers)
    first = create_validator(client, company_headers, email="a@empresa.com", name="Acceso A").json()["data"]
    assert create_validator(client, company_headers, email="b@empresa.com", name="Acceso B").status_code == 201
    page = client.get(URL, headers=company_headers).json()["data"]
    assert (page["active"], page["limit"], page["total"]) == (2, 2, 2)
    # El tercero no cabe: 409 con el límite (y nada se creó).
    full = create_validator(client, company_headers, email="c@empresa.com", name="Acceso C")
    assert (full.status_code, full.json()["code"]) == (409, "VALIDATOR_LIMIT_REACHED")
    assert full.json()["errors"][0]["details"] == {"limit": 2}
    assert full.json()["message"].startswith("Tu empresa llegó a su límite de 2 validadores activos.")
    english = create_validator(client, {**company_headers, **ENGLISH}, email="c@empresa.com", name="Acceso C").json()
    assert english["message"].startswith("Your company reached its limit of 2 active validators.")
    assert client.get(URL, headers=company_headers).json()["data"]["total"] == 2
    # Desactivar libera un lugar; reactivar lo vuelve a ocupar (si no hay lugar, 409).
    off = client.patch(f"{URL}/{first['id']}/status", json={"active": False}, headers=company_headers)
    assert off.status_code == 200 and client.get(URL, headers=company_headers).json()["data"]["active"] == 1
    assert create_validator(client, company_headers, email="c@empresa.com", name="Acceso C").status_code == 201
    back = client.patch(f"{URL}/{first['id']}/status", json={"active": True}, headers=company_headers)
    assert (back.status_code, back.json()["code"]) == (409, "VALIDATOR_LIMIT_REACHED")
    # Con un lugar más, sí; un cambio que no cambia nada no ocupa otro lugar.
    _set_limit(client, admin_headers, company_id, 3)
    assert client.patch(f"{URL}/{first['id']}/status", json={"active": True}, headers=company_headers).is_success
    again = client.patch(f"{URL}/{first['id']}/status", json={"active": True}, headers=company_headers)
    assert again.status_code == 200
    detail = client.get(f"{ADMIN_COMPANIES}/{company_id}", headers=admin_headers).json()["data"]
    assert (detail["max_validators"], detail["active_validators"]) == (3, 3)
    listed = client.get(ADMIN_COMPANIES, headers=admin_headers).json()["data"]["items"]
    assert [c["active_validators"] for c in listed if c["id"] == company_id] == [3]


def test_one_seat_left_reads_naturally_and_zero_seats_say_so(client, admin_headers, company_headers, monkeypatch):
    company_id = _company_id()
    _set_limit(client, admin_headers, company_id, 1)
    assert create_validator(client, company_headers, email="a@empresa.com").status_code == 201
    full = create_validator(client, company_headers, email="b@empresa.com").json()["message"]
    assert full.startswith("Tu empresa llegó a su límite de 1 validador activo.")
    # Sin lugares: el ADMIN apagó el módulo justo después de que la petición pasó su puerta (el candado lo ve).
    client.patch(f"{URL}/1/status", json={"active": False}, headers=company_headers)
    assert _set_limit(client, admin_headers, company_id, 0).status_code == 200
    monkeypatch.setattr("app.dependencies.validators_module", lambda user: user.company)
    late = create_validator(client, company_headers, email="c@empresa.com").json()
    assert late["code"] == "VALIDATOR_LIMIT_REACHED"
    assert late["message"] == (
        "Tu empresa ya no tiene lugares para validadores. Pide más al administrador de la plataforma."
    )


# ---------------------------------------------------------------- el límite nunca baja de los activos


def test_the_limit_never_drops_below_the_active_validators(client, admin_headers, company_headers):
    company_id = _company_id()
    ids = [create_validator(client, company_headers, email=f"v{i}@empresa.com").json()["data"]["id"] for i in range(2)]
    lower = _set_limit(client, admin_headers, company_id, 1)
    assert (lower.status_code, lower.json()["code"]) == (409, "VALIDATOR_LIMIT_BELOW_ACTIVE")
    error = lower.json()["errors"][0]
    assert (error["field"], error["details"]) == ("max_validators", {"active": 2})
    assert lower.json()["message"].startswith("La empresa tiene 2 validadores activos: el límite no puede ser menor.")
    english = client.put(
        f"{ADMIN_COMPANIES}/{company_id}", json={"max_validators": 0}, headers={**admin_headers, **ENGLISH}
    ).json()["message"]
    assert english.startswith("The company has 2 active validators: the limit can't be lower.")
    assert _set_limit(client, admin_headers, company_id, 2).status_code == 200  # justo los activos
    client.patch(f"{URL}/{ids[0]}/status", json={"active": False}, headers=company_headers)
    one = _set_limit(client, admin_headers, company_id, 0).json()["message"]
    assert one.startswith("La empresa tiene 1 validador activo: el límite no puede ser menor.")
    client.patch(f"{URL}/{ids[1]}/status", json={"active": False}, headers=company_headers)
    # Sin activos, el ADMIN puede apagar el módulo: la pantalla desaparece y sus APIs responden 403. Las cuentas
    # inactivas se conservan (al volver a darle lugares, la empresa las ve de nuevo).
    off = _set_limit(client, admin_headers, company_id, 0)
    assert off.status_code == 200 and off.json()["data"]["max_validators"] == 0
    assert "COMPANY_VALIDATORS" not in _screens(client, company_headers)
    assert client.get(URL, headers=company_headers).json()["code"] == "VALIDATORS_DISABLED"
    _set_limit(client, admin_headers, company_id, 1)
    assert client.get(URL, headers=company_headers).json()["data"]["total"] == 2
    # Límites del valor: no negativo y con tope (también al dar de alta).
    for value in (-1, 1001):
        assert _set_limit(client, admin_headers, company_id, value).status_code == 422
    bad = create_company(client, admin_headers, max_validators=1001)
    assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "max_validators"
    missing = _set_limit(client, admin_headers, 999, 1)
    assert (missing.status_code, missing.json()["code"]) == (404, "COMPANY_NOT_FOUND")


# ---------------------------------------------------------------- carreras


def test_two_simultaneous_validators_never_pass_the_limit(client, admin_headers, company_headers, monkeypatch):
    """Ambas altas pasaron sus revisiones; la otra toma el último lugar justo antes del candado de esta: esta lo ve al
    contar (con la empresa bloqueada) y responde 409, sin crear nada."""
    company_id = _company_id()
    _set_limit(client, admin_headers, company_id, 1)

    def competitor(other, *_):
        ValidatorService(other, company_id).create(ValidatorCreate.model_validate(_validator(9)))

    _wins_before(monkeypatch, BillingRepository, "lock_company", competitor)
    response = create_validator(client, company_headers)
    assert (response.status_code, response.json()["code"]) == (409, "VALIDATOR_LIMIT_REACHED")
    page = client.get(URL, headers=company_headers).json()["data"]
    assert (page["total"], page["active"], page["items"][0]["email"]) == (1, 1, "acceso9@empresa.com")


def test_lowering_the_limit_while_a_validator_is_created(client, admin_headers, monkeypatch):
    """El ADMIN baja el límite a 0 mientras la empresa da de alta uno: el candado los turna y el ADMIN ve al nuevo."""
    company_id = _company_id()

    def competitor(other, *_):
        ValidatorService(other, company_id).create(ValidatorCreate.model_validate(_validator(9)))

    _wins_before(monkeypatch, BillingRepository, "lock_company", competitor)
    response = _set_limit(client, admin_headers, company_id, 0)
    assert (response.status_code, response.json()["code"]) == (409, "VALIDATOR_LIMIT_BELOW_ACTIVE")


# ---------------------------------------------------------------- API de integración


def test_the_integration_api_follows_the_module(client, admin_headers, company_headers):
    company_id = _company_id()
    secret = new_key(client, company_headers)["secret"]
    create_validator(client, company_headers)
    listed = call(client, secret, "/validators").json()["data"]
    assert (listed["total"], listed["active"], listed["limit"]) == (1, 1, 10)
    client.patch(f"{URL}/{listed['items'][0]['id']}/status", json={"active": False}, headers=company_headers)
    _set_limit(client, admin_headers, company_id, 0)
    blocked = call(client, secret, "/validators")
    assert (blocked.status_code, blocked.json()["code"]) == (403, "VALIDATORS_DISABLED")
    assert blocked.json()["message"] == "La empresa de esta llave no tiene el módulo de validadores"
    assert call(client, secret, "/employees").status_code == 200  # lo demás de la llave sigue igual


# ---------------------------------------------------------------- cobro: un validador cuenta como un empleado


def test_each_change_of_a_validator_is_recorded_for_billing(client, company_headers):
    company_id = _company_id()
    first = create_validator(client, company_headers, email="a@empresa.com").json()["data"]["id"]
    second = create_validator(client, company_headers, email="b@empresa.com").json()["data"]["id"]
    for active in (False, False, True):  # desactivar dos veces escribe un solo evento
        client.patch(f"{URL}/{first}/status", json={"active": active}, headers=company_headers)
    client.patch(f"{URL}/{second}/status", json={"active": False}, headers=company_headers)
    assert client.delete(f"{URL}/{first}", headers=company_headers).status_code == 200  # activo: deja de contar
    assert client.delete(f"{URL}/{second}", headers=company_headers).status_code == 200  # ya inactivo: nada
    assert _events(company_id) == [
        (first, True),
        (second, True),
        (first, False),
        (first, True),
        (second, False),
        (first, False),
    ]


def _staff_validators(company_id: int, since: date, *ids: int, hour: int = 9) -> None:
    """Historial de altas de validadores escrito directo (el cobro solo lee el historial)."""
    with SessionLocal() as db:
        db.add_all(
            ValidatorStatusEvent(company_id=company_id, validator_id=i, active=True, occurred_at=at(since, hour))
            for i in ids
        )
        db.commit()


def _release_validator(company_id: int, validator_id: int, day: date) -> None:
    with SessionLocal() as db:
        db.add(
            ValidatorStatusEvent(
                company_id=company_id, validator_id=validator_id, active=False, occurred_at=at(day, 12)
            )
        )
        db.commit()


def _days(company_id: int) -> dict[date, tuple[int, int]]:
    with SessionLocal() as db:
        rows = db.execute(
            select(HeadcountDay.day, HeadcountDay.active_employees, HeadcountDay.active_validators).where(
                HeadcountDay.company_id == company_id
            )
        )
        return {day: (int(e), int(v)) for day, e, v in rows}


def _plan_company(client, admin_headers, max_validators: int = 0, **plan) -> int:
    billing = {**PLAN, "starts_on": NOV.isoformat(), **plan}
    response = create_company(client, admin_headers, max_validators=max_validators, billing=billing)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def test_validators_are_prorated_day_by_day_like_employees(client, admin_headers, monkeypatch):
    set_today(monkeypatch, NOV)
    company_id = _plan_company(client, admin_headers)
    hire(company_id, 2, date(2026, 10, 1))
    _staff_validators(company_id, date(2026, 10, 1), 1, 3)  # el 1 todo el mes; el 3 sale el 10
    _release_validator(company_id, 3, date(2026, 11, 10))
    _staff_validators(company_id, date(2026, 11, 21), 2)  # entra a mitad del periodo: desde el 21
    _release_validator(company_id, 1, date(2026, 11, 25))
    _staff_validators(company_id, date(2026, 11, 25), 1, hour=15)  # reactivado el mismo día: un día, no dos
    run_jobs(DEC)
    days = _days(company_id)
    assert days[date(2026, 11, 1)] == (2, 2) and days[date(2026, 11, 10)] == (2, 2)
    assert days[date(2026, 11, 11)] == (2, 1) and days[date(2026, 11, 21)] == (2, 2)
    assert days[date(2026, 11, 25)] == (2, 2) and days[date(2026, 11, 30)] == (2, 2)
    # 2 empleados × 30 + validadores (30 + 10 + 10) = 110 días-persona × $10 = $1 100 + IVA.
    with SessionLocal() as db:
        charge = db.scalars(select(Charge).where(Charge.company_id == company_id)).one()
    assert (charge.units, str(charge.subtotal), str(charge.total)) == (110, "1100.00", "1276.00")
    # El desglose que muestra la app: 50 de esos días-persona son de validadores (el dinero no cambia).
    detail = client.get(billing_url(company_id, f"/charges/{charge.id}"), headers=admin_headers).json()["data"]
    assert (detail["units"], detail["validator_units"]) == (110, 50)
    assert [(line["units"], line["validator_units"]) for line in detail["lines"]] == [(110, 50)]
    listed = client.get(billing_url(company_id, "/charges"), headers=admin_headers).json()["data"]["items"]
    assert [(c["units"], c["validator_units"]) for c in listed] == [(110, 50)]
    # Repetible: cerrar otra vez un día da lo mismo.
    with SessionLocal() as db:
        repo = BillingRepository(db)
        repo.close_headcount(date(2026, 11, 11), *billing_jobs.business_day_bounds(date(2026, 11, 11)))
        db.commit()
    assert _days(company_id) == days


def test_a_flat_plan_ignores_the_validators(client, admin_headers, monkeypatch):
    set_today(monkeypatch, NOV)
    company_id = _plan_company(client, admin_headers, pricing_mode="FLAT", unit_price="900")
    _staff_validators(company_id, date(2026, 10, 1), 1, 2, 3)
    run_jobs(DEC)
    with SessionLocal() as db:
        charge = db.scalars(select(Charge).where(Charge.company_id == company_id)).one()
    assert (charge.units, str(charge.subtotal), charge.validator_units) == (30, "900.00", None)  # sin desglose


def _money(preview: dict) -> dict:
    """La vista previa sin el desglose de los días-persona (lo único que cambia entre 3 empleados y 1 + 2
    validadores)."""
    if isinstance(preview, dict):
        return {key: _money(value) for key, value in preview.items() if key != "validator_units"}
    if isinstance(preview, list):
        return [_money(item) for item in preview]
    return preview


def test_estimate_usage_and_preview_count_the_validators(client, admin_headers, monkeypatch):
    set_today(monkeypatch, date(2026, 11, 10))
    company_id = _plan_company(client, admin_headers, max_validators=2)
    owner = login(client, *OWNER)
    assert create_employee(client, owner, number="E-1", email="e1@x.com").status_code == 201
    for i in range(2):
        assert create_validator(client, owner, email=f"v{i}@x.com").status_code == 201
    estimate = client.get(billing_url(company_id, "/estimate"), headers=admin_headers).json()["data"]
    # 1 empleado + 2 validadores = 3 al día: los 30 días se estiman con 3 (sin días cerrados todavía).
    assert (estimate["active_employees"], estimate["active_validators"]) == (1, 2)
    assert estimate["forecast"]["units"] == 90 and estimate["forecast"]["validator_units"] == 60
    assert estimate["accrued"] == {"units": 30, "validator_units": 20, "subtotal": "300.00"}
    assert [(line["units"], line["validator_units"]) for line in estimate["lines"]] == [(90, 60)]
    usage = client.get(f"/api/admin/usage/companies/{company_id}", headers=admin_headers).json()["data"]
    assert (usage["active_employees"], usage["active_validators"]) == (1, 2)
    rows = client.get("/api/admin/usage/companies", headers=admin_headers).json()["data"]["items"]
    assert [(r["active_employees"], r["active_validators"]) for r in rows if r["company_id"] == company_id] == [(1, 2)]
    # La vista previa: 3 empleados cuestan lo mismo que 1 empleado y 2 validadores.
    plan = {**PLAN, "starts_on": "2026-11-16"}
    mixed = {"plan": plan, "employees": 1, "validators": 2}
    preview = client.post("/api/admin/billing/preview", json=mixed, headers=admin_headers).json()["data"]
    only = client.post("/api/admin/billing/preview", json={"plan": plan, "employees": 3}, headers=admin_headers)
    assert preview["first"]["units"] == 45 and _money(preview) == _money(only.json()["data"])
    # Lo mismo en dinero; cambia el desglose de los días-persona (15 días × 2 validadores).
    assert (preview["first"]["validator_units"], only.json()["data"]["first"]["validator_units"]) == (30, 0)
    assert [line["validator_units"] for line in preview["recurring"]["lines"]] == [62]
    bad = client.post("/api/admin/billing/preview", json={**mixed, "validators": 1001}, headers=admin_headers)
    assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "validators"
