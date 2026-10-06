"""Cobranza (ADMIN): plan en el alta de la empresa, vista previa, cambio del plan, estimación en vivo,
resumen de la plataforma y la lista de empresas con su saldo."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import BillingPlan, User
from tests.billing_support import PLAN, billing_url, company_with_plan, hire, pay, run_jobs, set_today
from tests.conftest import COMPANY_EMAIL, create_company, create_employee, login

TODAY = date(2026, 11, 10)


def test_company_created_with_its_plan_in_the_same_operation(client, admin_headers, monkeypatch):
    set_today(monkeypatch, TODAY)
    discount = {"type": "PERCENT", "value": "10", "recurrence": "FIRST", "periods": 3}
    company_id = company_with_plan(client, admin_headers, trial_days=5, discount=discount, tax_rate="0")
    account = client.get(billing_url(company_id), headers=admin_headers).json()
    assert account["code"] == "BILLING_ACCOUNT"
    data = account["data"]
    assert data["status"] == "ACTIVE" and data["suspension"] is None and data["company_active"] is True
    plan = data["plan"]
    assert plan == {
        **plan,
        "pricing_mode": "PER_USER",
        "unit_price": "300.00",
        "price_period": "MONTH",
        "starts_on": TODAY.isoformat(),  # vacío = hoy
        "trial_ends_on": "2026-11-14",
        "next_cut_on": "2026-11-30",
        "tax_rate": "0.00",
        "grace_days": 5,
        "currency": "MXN",
        "discount": {"type": "PERCENT", "value": "10.00", "recurrence": "FIRST", "periods": 3},
    }
    assert plan["forecast_total"] == "0.00" and plan["forecast_at"]  # sin empleados aún
    assert data["balance"]["balance"] == "0.00" and data["balance"]["suspends_on"] is None
    with SessionLocal() as db:
        assert db.get(BillingPlan, company_id).updated_by == "superadmin@plataforma.com"
    detail = client.get(f"/api/admin/companies/{company_id}", headers=admin_headers).json()["data"]
    assert detail["billing_status"] == "ACTIVE" and detail["suspension_reason"] is None


def test_plan_validation_errors_point_to_their_field(client, admin_headers, monkeypatch):
    set_today(monkeypatch, TODAY)
    cases = [
        ({k: v for k, v in PLAN.items() if k != "unit_price"}, "billing.unit_price"),
        ({**PLAN, "discount": {"type": "AMOUNT", "value": "50", "recurrence": "EVERY"}}, "billing.discount.periods"),
        ({**PLAN, "discount": {"type": "PERCENT", "value": "150"}}, "billing.discount.value"),
        ({**PLAN, "starts_on": (TODAY + timedelta(days=400)).isoformat()}, "billing.starts_on"),
        ({**PLAN, "interval_months": 13}, "billing.interval_months"),
        ({**PLAN, "tax_rate": "100.5"}, "billing.tax_rate"),
    ]
    for billing, field in cases:
        response = create_company(client, admin_headers, billing=billing)
        assert response.status_code == 422 and response.json()["errors"][0]["field"] == field, response.text
    # Nada quedó a medias: el alta con un plan inválido no creó la empresa.
    assert client.get("/api/admin/companies", headers=admin_headers).json()["data"]["total"] == 1
    # "En todos los cargos" no lleva N (se ignora si viene).
    always = {**PLAN, "discount": {"type": "AMOUNT", "value": "50", "recurrence": "ALWAYS", "periods": 4}}
    created = create_company(client, admin_headers, billing=always)
    plan = client.get(billing_url(created.json()["data"]["id"]), headers=admin_headers).json()["data"]["plan"]
    assert plan["discount"]["periods"] is None
    # Sin plan: la empresa no se cobra hasta que el ADMIN se lo configure.
    bare = create_company(client, admin_headers, rfc="BAR120315AB1", admin_email="b@b.com")
    account = client.get(billing_url(bare.json()["data"]["id"]), headers=admin_headers).json()["data"]
    assert account["plan"] is None and account["grace_until"] is None


def test_preview_with_the_expected_headcount(client, admin_headers, monkeypatch):
    set_today(monkeypatch, TODAY)
    body = {"plan": {**PLAN, "starts_on": "2026-11-16"}, "employees": 10}
    response = client.post("/api/admin/billing/preview", json=body, headers=admin_headers)
    assert response.status_code == 200 and response.json()["code"] == "BILLING_PREVIEW"
    data = response.json()["data"]
    assert data["first"] == {
        **data["first"],
        "sequence": 1,
        "cut_on": "2026-11-30",
        "period_start": "2026-11-01",
        "billable_days": 15,
        "units": 150,
        "subtotal": "1500.00",
        "tax": "240.00",
        "total": "1740.00",
    }
    # Días-persona sin validadores: su desglose es 0 (la app solo muestra el total).
    line = {"month": "2026-11-01", "days": 15, "units": 150, "validator_units": 0, "amount": "1500.00"}
    assert data["first"]["lines"] == [line] and data["first"]["validator_units"] == 0
    assert data["recurring"]["total"] == "3480.00" and data["monthly_equivalent"] == "3480.00"
    demo = {"plan": {**PLAN, "trial_days": 60, "pricing_mode": "FLAT", "unit_price": "999"}, "employees": 0}
    demo_data = client.post("/api/admin/billing/preview", json=demo, headers=admin_headers).json()["data"]
    assert demo_data["first"] is None and demo_data["trial_ends_on"] == "2027-01-08"
    bad = client.post("/api/admin/billing/preview", json={"plan": PLAN, "employees": -1}, headers=admin_headers)
    assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "employees"


def test_changing_the_plan_applies_from_the_next_charge(client, admin_headers, company_headers, monkeypatch):
    set_today(monkeypatch, TODAY)
    with SessionLocal() as db:
        default_company = db.scalar(select(User.company_id).where(User.email == COMPANY_EMAIL))
    # La empresa de las pruebas no tiene plan (como las creadas antes de este módulo): se le crea.
    assert client.get(billing_url(default_company, "/estimate"), headers=admin_headers).status_code == 404
    saved = client.put(billing_url(default_company, "/plan"), json=PLAN, headers=admin_headers)
    assert saved.status_code == 200 and saved.json()["code"] == "BILLING_PLAN_SAVED"
    assert saved.json()["data"]["plan"]["next_cut_on"] == "2026-11-30"
    # Un cargo emitido: el siguiente corte sale del último emitido aunque el inicio cambie.
    hire(default_company, 2, date(2026, 11, 1))
    set_today(monkeypatch, date(2026, 12, 1))
    run_jobs(date(2026, 12, 1))
    changed = {**PLAN, "interval_months": 3, "starts_on": "2026-01-01", "unit_price": "600"}
    account = client.put(billing_url(default_company, "/plan"), json=changed, headers=admin_headers).json()["data"]
    assert account["plan"]["next_cut_on"] == "2027-02-28" and account["plan"]["unit_price"] == "600.00"
    charges = client.get(billing_url(default_company, "/charges"), headers=admin_headers).json()["data"]["items"]
    assert len(charges) == 1  # lo emitido no cambia: conserva el precio con que se calculó
    issued = client.get(billing_url(default_company, f"/charges/{charges[0]['id']}"), headers=admin_headers)
    assert issued.json()["data"]["unit_price"] == "300.00" and issued.json()["data"]["units"] == 42  # del 10 al 30
    missing = client.put(billing_url(999, "/plan"), json=PLAN, headers=admin_headers)
    assert missing.status_code == 404 and missing.json()["code"] == "COMPANY_NOT_FOUND"


def test_live_estimate_of_the_current_period(client, admin_headers, monkeypatch):
    set_today(monkeypatch, TODAY)
    company_id = company_with_plan(client, admin_headers, starts_on="2026-11-01")
    owner = login(client, "admin@panificadora.com", "Empresa1234")
    for i in range(3):
        number = f"E-{i}"
        assert create_employee(client, owner, number=number, email=f"e{i}@x.com").status_code == 201
    estimate = client.get(billing_url(company_id, "/estimate"), headers=admin_headers).json()
    assert estimate["code"] == "BILLING_ESTIMATE"
    data = estimate["data"]
    assert (data["sequence"], data["cut_on"], data["as_of"], data["days_total"], data["days_elapsed"]) == (
        1,
        "2026-11-30",
        TODAY.isoformat(),
        30,
        10,
    )
    assert data["active_employees"] == 3 and data["in_trial"] is False
    # Sin días cerrados todavía: los 10 días que van y los 20 que faltan se estiman con los 3 de hoy.
    assert data["accrued"] == {"units": 30, "validator_units": 0, "subtotal": "300.00"}
    assert data["forecast"] == {
        "units": 90,
        "validator_units": 0,
        "subtotal": "900.00",
        "discount": "0.00",
        "tax": "144.00",
        "total": "1044.00",
    }
    line = {"month": "2026-11-01", "days": 30, "units": 90, "validator_units": 0, "amount": "900.00", "projected": True}
    assert data["lines"] == [line]
    assert client.get(billing_url(999, "/estimate"), headers=admin_headers).status_code == 404


def test_platform_overview_and_company_list(client, admin_headers, monkeypatch):
    set_today(monkeypatch, date(2026, 12, 1))
    late = company_with_plan(client, admin_headers, starts_on="2026-11-01")
    fine = company_with_plan(client, admin_headers, rfc="FIN120315AB1", email="f@f.com", trial_days=200)
    hire(late, 5, date(2026, 10, 1))
    run_jobs(date(2026, 12, 1))  # noviembre: 5 empleados × 30 días × $10 = $1 500 + IVA = $1 740
    assert pay(client, admin_headers, late, "100.00", date(2026, 12, 1)).status_code == 201
    set_today(monkeypatch, date(2026, 12, 3))  # el cargo venció el 1 (ya está vencido)
    overview = client.get("/api/admin/billing/overview", headers=admin_headers).json()
    assert overview["code"] == "BILLING_OVERVIEW"
    data = overview["data"]
    assert data["last_cut_on"] == "2026-11-30"
    [pesos] = data["currencies"]  # todo en pesos: un solo grupo
    assert (pesos["currency"], pesos["last_cut_charges"], pesos["last_cut_total"]) == ("MXN", 1, "1740.00")
    assert (pesos["billed_month"], pesos["collected_month"]) == ("1740.00", "100.00")
    assert (pesos["outstanding"], pesos["overdue"], pesos["credit"]) == ("1640.00", "1640.00", "0.00")
    assert data["companies"] == {
        "total": 3,
        "with_plan": 2,
        "without_plan": 1,
        "overdue": 1,
        "suspended": 0,
        "in_trial": 1,
    }
    assert Decimal(pesos["forecast"]) >= 0
    listing = client.get("/api/admin/billing/companies", headers=admin_headers).json()
    assert listing["code"] == "BILLING_COMPANIES" and listing["data"]["total"] == 3
    first = listing["data"]["items"][0]  # la vencida primero
    assert first == {
        **first,
        "company_id": late,
        "status": "ACTIVE",
        "outstanding": "1640.00",
        "overdue": "1640.00",
        "open_charges": 1,
        "oldest_due_on": "2026-12-01",
        "last_payment_on": "2026-12-01",
        "next_cut_on": "2026-12-31",
        "currency": "MXN",
    }
    assert first["plan"] == {
        "pricing_mode": "PER_USER",
        "unit_price": "300.00",
        "price_period": "MONTH",
        "interval_months": 1,
    }
    without_plan = next(r for r in listing["data"]["items"] if r["plan"] is None)
    assert without_plan["outstanding"] == "0.00" and without_plan["next_cut_on"] is None
    assert without_plan["currency"] is None  # sin plan ni movimientos: sin moneda
    only = client.get("/api/admin/billing/companies?overdue=true", headers=admin_headers).json()["data"]
    assert [r["company_id"] for r in only["items"]] == [late]
    rest = client.get("/api/admin/billing/companies?overdue=false&search=panif", headers=admin_headers).json()["data"]
    assert [r["company_id"] for r in rest["items"]] == [fine]
    assert (
        client.get("/api/admin/billing/companies?status=SUSPENDED", headers=admin_headers).json()["data"]["total"] == 0
    )
    active = client.get("/api/admin/billing/companies?status=ACTIVE", headers=admin_headers).json()["data"]
    assert active["total"] == 3 and fine in [r["company_id"] for r in active["items"]]
    wrong = client.get("/api/admin/billing/companies?status=OTHER", headers=admin_headers)
    assert wrong.status_code == 422


def test_an_old_start_is_kept_but_a_new_one_must_be_within_a_year(client, admin_headers, monkeypatch):
    set_today(monkeypatch, TODAY)
    company_id = company_with_plan(client, admin_headers, starts_on="2025-11-15")
    # Un año y medio después el plan conserva su inicio al editar otra cosa.
    set_today(monkeypatch, date(2027, 5, 1))
    kept = client.put(
        billing_url(company_id, "/plan"),
        json={**PLAN, "starts_on": "2025-11-15", "grace_days": 3},
        headers=admin_headers,
    )
    assert kept.status_code == 200 and kept.json()["data"]["plan"]["grace_days"] == 3
    moved = client.put(
        billing_url(company_id, "/plan"), json={**PLAN, "starts_on": "2026-01-01"}, headers=admin_headers
    )
    assert moved.status_code == 422 and moved.json()["code"] == "PLAN_START_OUT_OF_RANGE"
    assert moved.json()["errors"][0]["field"] == "starts_on"
