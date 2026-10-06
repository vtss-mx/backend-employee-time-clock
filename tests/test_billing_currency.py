"""Cobro en varias monedas (MXN, USD, EUR): cada empresa se cobra en la moneda de su plan, sus cargos y pagos
la guardan, un pago debe ser en esa moneda, la moneda queda fija desde el primer movimiento y los totales de la
plataforma salen por moneda (nunca sumados ni convertidos). El redondeo usa los decimales del catálogo."""

from datetime import date
from decimal import Decimal

from sqlalchemy import insert, select, update

from app.core.database import SessionLocal
from app.models import CatalogCurrency, ChargeStatus, Payment, PaymentStatus
from app.services.billing_ledger import Ledger, currency_decimals
from app.services.catalog_service import clear_catalog_cache
from tests.billing_support import PLAN, billing_url, company_with_plan, hire, pay, run_jobs, set_today
from tests.conftest import create_company, owner_engine

NOV, DEC = date(2026, 11, 1), date(2026, 12, 1)
#: Un cliente del extranjero suele llevar IVA 0 (independiente de la moneda: lo decide cada empresa).
USD_PLAN = {"currency": "USD", "tax_rate": "0", "starts_on": NOV.isoformat()}


def _account(client, admin, company_id):
    return client.get(billing_url(company_id), headers=admin).json()["data"]


def _save_plan(client, admin, company_id, **changes):
    return client.put(billing_url(company_id, "/plan"), json={**PLAN, **changes}, headers=admin)


def _catalog_currency(code: str, **values) -> None:
    """Agrega o cambia una moneda del catálogo (solo en la base de esta prueba). Con el dueño de la base: en
    PostgreSQL la API (y las sesiones de las pruebas) solo leen los catálogos."""
    table = CatalogCurrency.__table__
    with owner_engine.begin() as conn:
        if conn.execute(select(table.c.code).where(table.c.code == code)).first() is None:
            conn.execute(insert(table).values(code=code, name=code, symbol="¤", sort_order=9, **values))
        else:
            conn.execute(update(table).where(table.c.code == code).values(**values))
    clear_catalog_cache()


def test_a_company_billed_in_dollars_end_to_end(client, admin_headers, monkeypatch):
    set_today(monkeypatch, NOV)
    company_id = company_with_plan(client, admin_headers, rfc="DOL120315AB1", email="d@dolares.com", **USD_PLAN)
    account = _account(client, admin_headers, company_id)
    assert (account["currency"], account["currency_locked"], account["plan"]["currency"]) == ("USD", False, "USD")
    estimate = client.get(billing_url(company_id, "/estimate"), headers=admin_headers).json()["data"]
    assert estimate["currency"] == "USD"
    # Noviembre: 2 empleados × 30 días × $10 = $600 USD, sin IVA.
    hire(company_id, 2, date(2026, 10, 1))
    set_today(monkeypatch, DEC)
    run_jobs(DEC)
    charge = client.get(billing_url(company_id, "/charges"), headers=admin_headers).json()["data"]["items"][0]
    assert (charge["currency"], charge["total"], charge["tax"]) == ("USD", "600.00", "0.00")
    detail = client.get(billing_url(company_id, f"/charges/{charge['id']}"), headers=admin_headers).json()["data"]
    assert detail["currency"] == "USD"
    # Un pago en pesos no paga una deuda en dólares (ni se convierte).
    pesos = pay(client, admin_headers, company_id, "600.00", DEC)
    assert pesos.status_code == 422 and pesos.json()["code"] == "CURRENCY_MISMATCH"
    assert pesos.json()["errors"][0]["field"] == "currency" and "USD" in pesos.json()["message"]
    missing = client.post(
        billing_url(company_id, "/payments"),
        data={"amount": "600.00", "paid_on": DEC.isoformat(), "method": "TRANSFER"},
        headers=admin_headers,
    )
    assert missing.status_code == 422 and missing.json()["errors"][0]["field"] == "currency"
    dollars = pay(client, admin_headers, company_id, "600.00", DEC, currency="USD").json()["data"]
    assert dollars["payment"]["currency"] == "USD" and dollars["applied"][0]["amount"] == "600.00"
    assert dollars["account"]["currency_locked"] is True
    payments = client.get(billing_url(company_id, "/payments"), headers=admin_headers).json()["data"]["items"]
    assert [p["currency"] for p in payments] == ["USD"]
    statement = client.get(billing_url(company_id, "/statement"), headers=admin_headers).json()["data"]["items"]
    assert {entry["currency"] for entry in statement} == {"USD"}
    # Con movimientos, la moneda queda fija; cambiar lo demás del plan sigue permitido.
    locked = _save_plan(client, admin_headers, company_id, **{**USD_PLAN, "currency": "EUR"})
    assert locked.status_code == 422 and locked.json()["code"] == "CURRENCY_LOCKED"
    assert locked.json()["errors"][0]["field"] == "currency"
    kept = _save_plan(client, admin_headers, company_id, **{**USD_PLAN, "grace_days": 3})
    assert kept.status_code == 200 and kept.json()["data"]["plan"]["grace_days"] == 3
    listing = client.get("/api/admin/billing/companies", headers=admin_headers).json()["data"]["items"]
    assert next(r for r in listing if r["company_id"] == company_id)["currency"] == "USD"
    usage = client.get(f"/api/admin/usage/companies/{company_id}", headers=admin_headers).json()["data"]
    assert usage["billing"]["currency"] == "USD"


def test_the_currency_changes_freely_until_the_first_movement(client, admin_headers, monkeypatch):
    set_today(monkeypatch, NOV)
    company_id = company_with_plan(client, admin_headers)
    euros = _save_plan(client, admin_headers, company_id, currency="EUR")
    assert euros.status_code == 200 and euros.json()["data"]["currency"] == "EUR"
    assert euros.json()["data"]["currency_locked"] is False
    unknown = _save_plan(client, admin_headers, company_id, currency="XYZ")
    assert unknown.status_code == 422 and unknown.json()["code"] == "CURRENCY_INVALID"
    assert unknown.json()["errors"][0]["field"] == "currency"
    lowercase = _save_plan(client, admin_headers, company_id, currency="usd")
    assert lowercase.status_code == 422 and lowercase.json()["errors"][0]["field"] == "currency"
    created = create_company(
        client, admin_headers, rfc="XYZ120315AB1", admin_email="x@x.com", billing={**PLAN, "currency": "XYZ"}
    )
    assert created.status_code == 422 and created.json()["errors"][0]["field"] == "billing.currency"
    # Una moneda desactivada ya no se ofrece, pero quien ya la tiene la conserva.
    _catalog_currency("EUR", active=False)
    assert _save_plan(client, admin_headers, company_id, currency="EUR", grace_days=7).status_code == 200
    other = company_with_plan(client, admin_headers, rfc="OTR120315AB1", email="o@o.com")
    retired = _save_plan(client, admin_headers, other, currency="EUR")
    assert retired.status_code == 422 and retired.json()["code"] == "CURRENCY_INVALID"


def test_a_payment_without_plan_fixes_the_currency(client, admin_headers, monkeypatch):
    set_today(monkeypatch, DEC)
    response = create_company(client, admin_headers, rfc="SIN120315AB1", admin_email="s@s.com")
    company_id = response.json()["data"]["id"]
    assert _account(client, admin_headers, company_id)["currency"] is None
    unknown = pay(client, admin_headers, company_id, "100.00", DEC, currency="XYZ")
    assert unknown.status_code == 422 and unknown.json()["code"] == "CURRENCY_INVALID"
    advance = pay(client, admin_headers, company_id, "100.00", DEC, currency="USD")
    assert advance.status_code == 201 and advance.json()["data"]["payment"]["unapplied"] == "100.00"
    account = _account(client, admin_headers, company_id)
    assert (account["plan"], account["currency"], account["currency_locked"]) == (None, "USD", True)
    assert account["balance"]["credit"] == "100.00"
    listing = client.get("/api/admin/billing/companies", headers=admin_headers).json()["data"]["items"]
    assert next(r for r in listing if r["company_id"] == company_id)["currency"] == "USD"
    pesos = pay(client, admin_headers, company_id, "50.00", DEC, currency="MXN")
    assert pesos.status_code == 422 and pesos.json()["code"] == "CURRENCY_MISMATCH"
    assert _save_plan(client, admin_headers, company_id, currency="MXN").json()["code"] == "CURRENCY_LOCKED"
    plan = _save_plan(client, admin_headers, company_id, currency="USD").json()["data"]
    assert plan["plan"]["currency"] == "USD" and plan["currency"] == "USD"


def test_platform_totals_are_reported_per_currency(client, admin_headers, monkeypatch):
    set_today(monkeypatch, NOV)
    empty = client.get("/api/admin/billing/overview", headers=admin_headers).json()["data"]
    assert (empty["currencies"], empty["last_cut_on"], empty["companies"]["with_plan"]) == ([], None, 0)
    pesos = company_with_plan(client, admin_headers, starts_on=NOV.isoformat())
    dollars = company_with_plan(client, admin_headers, rfc="DOL120315AB1", email="d@d.com", **USD_PLAN)
    company_with_plan(client, admin_headers, rfc="EUR120315AB1", email="e@e.com", currency="EUR", trial_days=200)
    hire(pesos, 5, date(2026, 10, 1))
    hire(dollars, 2, date(2026, 10, 1), first_id=20_000)
    set_today(monkeypatch, DEC)
    run_jobs(DEC)  # $1 740 MXN (con IVA) y $600 USD (sin IVA)
    assert pay(client, admin_headers, pesos, "100.00", DEC).status_code == 201
    set_today(monkeypatch, date(2026, 12, 3))
    data = client.get("/api/admin/billing/overview", headers=admin_headers).json()["data"]
    assert data["last_cut_on"] == "2026-11-30"
    assert [group["currency"] for group in data["currencies"]] == ["MXN", "USD", "EUR"]  # orden del catálogo
    mxn, usd, eur = data["currencies"]
    assert mxn == {
        **mxn,
        "companies": 1,
        "billed_month": "1740.00",
        "collected_month": "100.00",
        "outstanding": "1640.00",
        "overdue": "1640.00",
        "overdue_companies": 1,
        "credit": "0.00",
        "last_cut_charges": 1,
        "last_cut_total": "1740.00",
    }
    assert usd == {
        **usd,
        "companies": 1,
        "billed_month": "600.00",
        "collected_month": "0.00",
        "outstanding": "600.00",
        "overdue": "600.00",
        "overdue_companies": 1,
        "last_cut_total": "600.00",
    }
    assert eur == {
        **eur,
        "companies": 1,
        "billed_month": "0.00",
        "outstanding": "0.00",
        "overdue_companies": 0,
        "last_cut_charges": 0,
        "last_cut_total": "0.00",
    }
    assert all(Decimal(group["forecast"]) >= 0 for group in data["currencies"])
    assert data["companies"] == {
        "total": 4,
        "with_plan": 3,
        "without_plan": 1,
        "overdue": 2,
        "suspended": 0,
        "in_trial": 1,
    }


def test_a_payment_never_settles_a_charge_in_another_currency(client, admin_headers, monkeypatch):
    """El reparto no confía en que la empresa tenga una sola moneda: un pago solo cubre cargos de la suya."""
    set_today(monkeypatch, NOV)
    company_id = company_with_plan(client, admin_headers, starts_on=NOV.isoformat())
    hire(company_id, 1, date(2026, 10, 1))
    set_today(monkeypatch, DEC)
    run_jobs(DEC)
    with SessionLocal() as db:
        db.add(
            Payment(
                company_id=company_id,
                currency="USD",
                amount=Decimal("5000"),
                paid_on=DEC,
                method="TRANSFER",
                status=PaymentStatus.CONFIRMED,
                applied=Decimal("0"),
            )
        )
        db.flush()
        Ledger(db).lock(company_id)
        assert Ledger(db).settle(company_id) == []
        db.commit()
    charge = client.get(billing_url(company_id, "/charges"), headers=admin_headers).json()["data"]["items"][0]
    assert (charge["status"], charge["paid"]) == (ChargeStatus.OPEN, "0.00")


def test_rounding_and_amounts_follow_the_currency_decimals(client, admin_headers, monkeypatch):
    """A prueba del futuro: una moneda sin centavos (agregada al catálogo solo en esta prueba) redondea a enteros
    y rechaza montos que no se pueden escribir en ella."""
    set_today(monkeypatch, NOV)
    _catalog_currency("JPY", decimals=0, active=True)
    assert currency_decimals("JPY") == 0 and currency_decimals("ZZZ") == 2  # sin catálogo: centavos
    plan = {**PLAN, "currency": "JPY", "unit_price": "100", "starts_on": "2026-11-17"}
    preview = client.post("/api/admin/billing/preview", json={"plan": plan, "employees": 1}, headers=admin_headers)
    first = preview.json()["data"]["first"]
    # 14 días × 100/30 = 46.67 → 47; IVA 7.52 → 8 (en pesos serían 46.67 + 7.47).
    assert (first["subtotal"], first["tax"], first["total"]) == ("47.00", "8.00", "55.00")
    assert preview.json()["data"]["currency"] == "JPY"
    fraction = {**plan, "discount": {"type": "AMOUNT", "value": "10.50", "recurrence": "ALWAYS"}}
    rejected = client.post("/api/admin/billing/preview", json={"plan": fraction, "employees": 1}, headers=admin_headers)
    assert rejected.status_code == 422 and rejected.json()["code"] == "AMOUNT_DECIMALS"
    assert rejected.json()["errors"][0]["field"] == "plan.discount.value"
    unknown = client.post(
        "/api/admin/billing/preview", json={"plan": {**PLAN, "currency": "XYZ"}, "employees": 1}, headers=admin_headers
    )
    assert unknown.status_code == 422 and unknown.json()["errors"][0]["field"] == "plan.currency"
    company_id = company_with_plan(client, admin_headers, **plan)
    cents = pay(client, admin_headers, company_id, "10.50", NOV, currency="JPY")
    assert cents.status_code == 422 and cents.json()["code"] == "AMOUNT_DECIMALS"
    assert cents.json()["errors"][0]["field"] == "amount"
    assert pay(client, admin_headers, company_id, "10", NOV, currency="JPY").status_code == 201
