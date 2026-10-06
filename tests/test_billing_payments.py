"""Cargos y pagos (ADMIN): cada pago se aplica al cargo abierto más antiguo y lo que sobra queda a favor;
anular un pago o un cargo devuelve el dinero a su origen; la suspensión (automática o manual) cierra al
momento las sesiones de la empresa y pagar lo vencido la reactiva."""

import base64
from datetime import date

from app.core.config import settings
from tests.billing_support import PDF, billing_url, company_with_plan, hire, pay, run_jobs, set_today
from tests.conftest import login

NOV, DEC, JAN = date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)


def _charges(client, admin, company_id, query=""):
    return client.get(billing_url(company_id, f"/charges{query}"), headers=admin).json()["data"]


def _payments(client, admin, company_id, query=""):
    return client.get(billing_url(company_id, f"/payments{query}"), headers=admin).json()["data"]


def _balance(client, admin, company_id):
    return client.get(billing_url(company_id), headers=admin).json()["data"]["balance"]


def _issued_company(client, admin, monkeypatch, *, employees=10) -> int:
    """Empresa con un cargo de noviembre emitido el 1 de diciembre: 10 empleados × 30 días × $10 + IVA."""
    set_today(monkeypatch, NOV)
    company_id = company_with_plan(client, admin, starts_on=NOV.isoformat())
    hire(company_id, employees, date(2026, 10, 1))
    set_today(monkeypatch, DEC)
    run_jobs(DEC)
    return company_id


def test_a_month_of_billing_from_charge_to_credit(client, admin_headers, monkeypatch):
    company_id = _issued_company(client, admin_headers, monkeypatch)
    charges = _charges(client, admin_headers, company_id)
    assert charges["total"] == 1
    charge = charges["items"][0]
    assert charge == {
        **charge,
        "sequence": 1,
        "cut_on": "2026-11-30",
        "period_start": "2026-11-01",
        "issued_on": "2026-12-01",
        "due_on": "2026-12-01",
        "billable_days": 30,
        "units": 300,
        "subtotal": "3000.00",
        "discount": "0.00",
        "tax": "480.00",
        "total": "3480.00",
        "paid": "0.00",
        "balance": "3480.00",
        "status": "OPEN",
        "overdue": False,
    }
    # Un abono con comprobante y luego el resto con sobrante: lo que sobra queda a favor.
    first = pay(
        client,
        admin_headers,
        company_id,
        "1000.00",
        DEC,
        reference="SPEI 123",
        files={"receipt": ("r.pdf", PDF, "application/pdf")},
    )
    assert first.status_code == 201 and first.json()["code"] == "PAYMENT_REGISTERED"
    result = first.json()["data"]
    assert result["applied"] == [
        {"charge_id": charge["id"], "sequence": 1, "cut_on": "2026-11-30", "amount": "1000.00"}
    ]
    assert result["payment"] == {
        **result["payment"],
        "amount": "1000.00",
        "applied": "1000.00",
        "unapplied": "0.00",
        "status": "CONFIRMED",
        "reference": "SPEI 123",
        "recorded_by": "superadmin@plataforma.com",
        "receipt": {"file_name": "r.pdf", "content_type": "application/pdf", "size": len(PDF)},
    }
    assert result["account"]["balance"]["outstanding"] == "2480.00" and result["reactivated"] is False
    second = pay(client, admin_headers, company_id, "3000.00", DEC, note="  ")
    assert second.json()["data"]["applied"][0]["amount"] == "2480.00"
    assert (
        second.json()["data"]["payment"]["unapplied"] == "520.00" and second.json()["data"]["payment"]["note"] is None
    )
    balance = _balance(client, admin_headers, company_id)
    assert (balance["outstanding"], balance["credit"], balance["balance"]) == ("0.00", "520.00", "-520.00")
    assert _charges(client, admin_headers, company_id, "?status=PAID")["total"] == 1
    # Diciembre: el saldo a favor se aplica solo al nuevo cargo.
    set_today(monkeypatch, JAN)
    run_jobs(JAN)
    december = _charges(client, admin_headers, company_id, "?status=OPEN")["items"][0]
    assert (december["sequence"], december["total"], december["paid"]) == (2, "3480.00", "520.00")
    detail = client.get(billing_url(company_id, f"/charges/{december['id']}"), headers=admin_headers).json()
    assert detail["code"] == "CHARGE_FOUND"
    line = {"month": "2026-12-01", "days": 31, "units": 310, "validator_units": 0, "amount": "3000.00"}
    assert detail["data"]["lines"] == [line] and detail["data"]["validator_units"] == 0
    assert detail["data"]["allocations"][0]["amount"] == "520.00" and detail["data"]["tax_rate"] == "16.00"
    # El comprobante, en base64.
    payment_id = result["payment"]["id"]
    receipt = client.get(billing_url(company_id, f"/payments/{payment_id}/receipt"), headers=admin_headers).json()
    assert receipt["code"] == "PAYMENT_RECEIPT" and base64.b64decode(receipt["data"]["data"]) == PDF
    no_receipt = client.get(
        billing_url(company_id, f"/payments/{second.json()['data']['payment']['id']}/receipt"), headers=admin_headers
    )
    assert no_receipt.status_code == 404 and no_receipt.json()["code"] == "RECEIPT_NOT_FOUND"
    listing = _payments(client, admin_headers, company_id)
    assert listing["total"] == 2 and listing["items"][0]["receipt"] is None  # el más reciente primero
    assert _payments(client, admin_headers, company_id, "?status=VOID")["total"] == 0


def test_voiding_a_payment_or_a_charge_returns_the_money(client, admin_headers, monkeypatch):
    company_id = _issued_company(client, admin_headers, monkeypatch)
    small = pay(client, admin_headers, company_id, "1000.00", DEC).json()["data"]["payment"]
    big = pay(client, admin_headers, company_id, "3000.00", DEC).json()["data"]["payment"]
    set_today(monkeypatch, JAN)
    run_jobs(JAN)
    # Anular el pago grande: noviembre vuelve a deber $2 480 y diciembre todo (sin otro saldo a favor).
    voided = client.post(
        billing_url(company_id, f"/payments/{big['id']}/void"), json={"reason": "Pago rebotado"}, headers=admin_headers
    )
    assert voided.status_code == 200 and voided.json()["code"] == "PAYMENT_VOIDED"
    data = voided.json()["data"]
    assert data["payment"]["status"] == "VOID" and data["payment"]["applied"] == "0.00"
    assert (
        data["payment"]["void_reason"] == "Pago rebotado"
        and data["payment"]["voided_by"] == "superadmin@plataforma.com"
    )
    assert data["account"]["balance"]["outstanding"] == "5960.00" and data["account"]["balance"]["credit"] == "0.00"
    again = client.post(
        billing_url(company_id, f"/payments/{big['id']}/void"), json={"reason": "Otra vez"}, headers=admin_headers
    )
    assert again.status_code == 409 and again.json()["code"] == "PAYMENT_ALREADY_VOID"
    # Anular noviembre: el pago chico queda a favor y cubre parte de diciembre.
    november = _charges(client, admin_headers, company_id, "?status=OPEN")["items"][-1]
    void = client.post(
        billing_url(company_id, f"/charges/{november['id']}/void"),
        json={"reason": "Cobrado de más"},
        headers=admin_headers,
    )
    assert void.status_code == 200 and void.json()["code"] == "CHARGE_VOIDED"
    assert void.json()["data"]["charge"]["status"] == "VOID" and void.json()["data"]["charge"]["paid"] == "0.00"
    assert void.json()["data"]["charge"]["voided_by"] == "superadmin@plataforma.com"
    balance = void.json()["data"]["account"]["balance"]
    assert (balance["outstanding"], balance["credit"], balance["charged"]) == ("2480.00", "0.00", "3480.00")
    payments = {p["id"]: p for p in _payments(client, admin_headers, company_id)["items"]}
    assert payments[small["id"]]["applied"] == "1000.00"  # ahora cubre diciembre
    twice = client.post(
        billing_url(company_id, f"/charges/{november['id']}/void"), json={"reason": "Otra vez"}, headers=admin_headers
    )
    assert twice.status_code == 409 and twice.json()["code"] == "CHARGE_ALREADY_VOID"
    # Estado de cuenta: cargos vigentes y pagos confirmados con el saldo acumulado (lo más reciente primero).
    statement = client.get(billing_url(company_id, "/statement"), headers=admin_headers).json()
    assert statement["code"] == "BILLING_STATEMENT"
    entries = statement["data"]["items"]
    assert [(e["kind"], e["debit"], e["credit"], e["balance"]) for e in entries] == [
        ("CHARGE", "3480.00", "0.00", "2480.00"),
        ("PAYMENT", "0.00", "1000.00", "-1000.00"),
    ]
    assert entries[0]["description"].startswith("Cargo 2 · 01/12/2026 al 31/12/2026")
    assert entries[1]["description"] == "Pago · Transferencia"


def test_payment_validation_and_not_found(client, admin_headers, monkeypatch):
    company_id = _issued_company(client, admin_headers, monkeypatch)
    future = pay(client, admin_headers, company_id, "10", date(2026, 12, 2))
    assert future.status_code == 422 and future.json()["code"] == "PAYMENT_DATE_IN_FUTURE"
    method = pay(client, admin_headers, company_id, "10", DEC, method="BITCOIN")
    assert method.status_code == 422 and method.json()["errors"][0]["field"] == "method"
    zero = pay(client, admin_headers, company_id, "0", DEC)
    assert zero.status_code == 422 and zero.json()["errors"][0]["field"] == "amount"
    exe = pay(
        client, admin_headers, company_id, "10", DEC, files={"receipt": ("virus.exe", b"MZ\x90\x00", "application/pdf")}
    )
    assert exe.status_code == 422 and exe.json()["code"] == "RECEIPT_TYPE_NOT_ALLOWED"
    for name, content, kind in (
        ("foto.png", b"\x89PNG\r\n\x1a\n" + b"0" * 10, "image/png"),
        ("foto.jpg", b"\xff\xd8\xff\xe0" + b"0" * 10, "image/jpeg"),
        ("C:\\scans\\foto.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    ):
        ok = pay(
            client, admin_headers, company_id, "1", DEC, files={"receipt": (name, content, "application/octet-stream")}
        )
        assert ok.status_code == 201 and ok.json()["data"]["payment"]["receipt"]["content_type"] == kind
    assert ok.json()["data"]["payment"]["receipt"]["file_name"] == "foto.webp"  # sin la ruta del equipo
    monkeypatch.setattr(settings, "BILLING_RECEIPT_MAX_MB", 0.00001)
    big = pay(client, admin_headers, company_id, "10", DEC, files={"receipt": ("r.pdf", PDF * 10, "application/pdf")})
    assert big.status_code == 413 and big.json()["code"] == "RECEIPT_TOO_LARGE"
    for url in ("/charges/999", "/payments/999/receipt"):
        response = client.get(billing_url(company_id, url), headers=admin_headers)
        assert response.status_code == 404 and response.json()["code"] in ("CHARGE_NOT_FOUND", "PAYMENT_NOT_FOUND")
    for url in ("/charges/999/void", "/payments/999/void"):
        response = client.post(billing_url(company_id, url), json={"reason": "No existe"}, headers=admin_headers)
        assert response.status_code == 404
    short = client.post(billing_url(company_id, "/suspend"), json={"reason": "  a  "}, headers=admin_headers)
    assert short.status_code == 422 and short.json()["errors"][0]["field"] == "reason"
    for url in ("", "/charges", "/payments", "/statement"):
        assert client.get(billing_url(999, url), headers=admin_headers).json()["code"] == "COMPANY_NOT_FOUND"
    assert pay(client, admin_headers, 999, "10", DEC).status_code == 404


def test_automatic_suspension_closes_sessions_and_payment_reactivates(client, admin_headers, monkeypatch):
    company_id = _issued_company(client, admin_headers, monkeypatch)
    owner = login(client, "admin@panificadora.com", "Empresa1234")
    # Gracia de 5 días después del vencimiento (1 de diciembre): el 6 aún no; el 7 sí.
    set_today(monkeypatch, date(2026, 12, 6))
    assert _balance(client, admin_headers, company_id)["suspends_on"] == "2026-12-07"
    assert run_jobs(date(2026, 12, 6))["empresas suspendidas por falta de pago"] == 0
    assert run_jobs(date(2026, 12, 7))["empresas suspendidas por falta de pago"] == 1
    kicked = client.get("/api/users/me", headers=owner)
    assert kicked.status_code == 401 and kicked.json()["code"] == "COMPANY_SUSPENDED"
    account = client.get(billing_url(company_id), headers=admin_headers).json()["data"]
    assert account["status"] == "SUSPENDED" and account["suspension"]["reason"] == "NON_PAYMENT"
    assert account["suspension"]["suspended_by"] is None and "5 días de gracia" in account["suspension"]["note"]
    assert account["balance"]["suspends_on"] is None
    listing = client.get("/api/admin/billing/companies?status=SUSPENDED", headers=admin_headers).json()["data"]
    assert [r["company_id"] for r in listing["items"]] == [company_id] and listing["items"][0][
        "suspension_reason"
    ] == "NON_PAYMENT"
    # Un abono que no cubre lo vencido no la reactiva; cubrirlo sí.
    set_today(monkeypatch, date(2026, 12, 8))
    assert pay(client, admin_headers, company_id, "100", date(2026, 12, 8)).json()["data"]["reactivated"] is False
    paid = pay(client, admin_headers, company_id, "3380", date(2026, 12, 8)).json()["data"]
    assert paid["reactivated"] is True and paid["account"]["status"] == "ACTIVE"
    assert (
        client.post("/api/auth/login", json={"email": "admin@panificadora.com", "password": "Empresa1234"}).status_code
        == 200
    )


def test_manual_suspension_and_reactivation(client, admin_headers, monkeypatch):
    company_id = _issued_company(client, admin_headers, monkeypatch)
    owner = login(client, "admin@panificadora.com", "Empresa1234")
    suspended = client.post(
        billing_url(company_id, "/suspend"), json={"reason": "Incumplimiento de contrato"}, headers=admin_headers
    )
    assert suspended.status_code == 200 and suspended.json()["code"] == "COMPANY_SUSPENDED_BY_ADMIN"
    data = suspended.json()["data"]
    assert data["status"] == "SUSPENDED" and data["suspension"]["reason"] == "MANUAL"
    assert (
        data["suspension"]["note"] == "Incumplimiento de contrato"
        and data["suspension"]["suspended_by"] == "superadmin@plataforma.com"
    )
    assert client.get("/api/users/me", headers=owner).json()["code"] == "COMPANY_SUSPENDED"
    again = client.post(billing_url(company_id, "/suspend"), json={"reason": "Otra vez"}, headers=admin_headers)
    assert again.status_code == 409 and again.json()["code"] == "COMPANY_ALREADY_SUSPENDED"
    # Pagar no levanta una suspensión manual: solo el ADMIN.
    assert pay(client, admin_headers, company_id, "3480", DEC).json()["data"]["reactivated"] is False
    detail = client.get(f"/api/admin/companies/{company_id}", headers=admin_headers).json()["data"]
    assert detail["billing_status"] == "SUSPENDED" and detail["suspension_reason"] == "MANUAL"
    back = client.post(billing_url(company_id, "/reactivate"), json={"note": "Acuerdo firmado"}, headers=admin_headers)
    assert back.status_code == 200 and back.json()["code"] == "COMPANY_REACTIVATED"
    assert back.json()["data"]["status"] == "ACTIVE" and back.json()["data"]["grace_until"] == "2026-12-06"
    not_suspended = client.post(billing_url(company_id, "/reactivate"), json={}, headers=admin_headers)
    assert not_suspended.status_code == 409 and not_suspended.json()["code"] == "COMPANY_NOT_SUSPENDED"
    assert (
        client.post("/api/auth/login", json={"email": "admin@panificadora.com", "password": "Empresa1234"}).status_code
        == 200
    )


def test_manual_reactivation_gives_a_new_grace_period(client, admin_headers, monkeypatch):
    company_id = _issued_company(client, admin_headers, monkeypatch)
    run_jobs(date(2026, 12, 7))  # suspendida por falta de pago
    set_today(monkeypatch, date(2026, 12, 10))
    back = client.post(billing_url(company_id, "/reactivate"), json={"note": None}, headers=admin_headers).json()[
        "data"
    ]
    assert back["grace_until"] == "2026-12-15" and back["balance"]["suspends_on"] == "2026-12-16"
    assert run_jobs(date(2026, 12, 15))["empresas suspendidas por falta de pago"] == 0  # aún en su gracia extra
    assert run_jobs(date(2026, 12, 16))["empresas suspendidas por falta de pago"] == 1


def test_voiding_the_overdue_charge_reactivates_a_company_suspended_for_non_payment(client, admin_headers, monkeypatch):
    company_id = _issued_company(client, admin_headers, monkeypatch)
    run_jobs(date(2026, 12, 7))
    charge = _charges(client, admin_headers, company_id)["items"][0]
    void = client.post(
        billing_url(company_id, f"/charges/{charge['id']}/void"),
        json={"reason": "Error de captura"},
        headers=admin_headers,
    )
    assert void.json()["data"]["account"]["status"] == "ACTIVE"


def test_a_company_with_charges_or_payments_is_never_deleted(client, admin_headers, monkeypatch):
    set_today(monkeypatch, DEC)
    company_id = company_with_plan(client, admin_headers)
    assert pay(client, admin_headers, company_id, "50", DEC).json()["data"]["applied"] == []  # todo a favor
    deleted = client.delete(f"/api/admin/companies/{company_id}", headers=admin_headers)
    assert deleted.status_code == 409 and deleted.json()["code"] == "COMPANY_HAS_BILLING"
    clean = company_with_plan(client, admin_headers, rfc="CLN120315AB1", email="c@c.com")
    assert client.delete(f"/api/admin/companies/{clean}", headers=admin_headers).status_code == 200


def test_voiding_a_payment_that_was_all_credit(client, admin_headers, monkeypatch):
    set_today(monkeypatch, DEC)
    company_id = company_with_plan(client, admin_headers)
    payment = pay(client, admin_headers, company_id, "75", DEC).json()["data"]["payment"]
    assert _balance(client, admin_headers, company_id)["credit"] == "75.00"
    voided = client.post(
        billing_url(company_id, f"/payments/{payment['id']}/void"), json={"reason": "Duplicado"}, headers=admin_headers
    )
    assert voided.json()["data"]["account"]["balance"]["credit"] == "0.00"
    assert voided.json()["data"]["payment"]["unapplied"] == "0.00"  # anulado: ya no queda a favor
