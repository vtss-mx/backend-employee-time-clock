"""Consumo (ADMIN): toda la plataforma, cada empresa (con su almacenamiento y lo que paga) y el detalle
por cuenta y por ruta, en un rango de días acotado."""

from datetime import date

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import StorageSnapshot, User
from app.services.usage_meter import UsageMeter
from tests.billing_support import company_with_plan, set_today
from tests.conftest import COMPANY_EMAIL, create_employee

NOV3, NOV4 = date(2026, 11, 3), date(2026, 11, 4)


def _company_and_user() -> tuple[int, int]:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == COMPANY_EMAIL))
        return int(user.company_id), int(user.id)


def _traffic(company_id: int, user_id: int, employee_user: int, other: int) -> None:
    meter = UsageMeter(1000)
    hit = {"bytes_in": 1024, "bytes_out": 4096, "duration_ms": 20.0}
    for day in (NOV3, NOV4):
        meter.record(company_id=company_id, user_id=user_id, route="GET /api/employees", status=200, day=day, **hit)
    meter.record(
        company_id=company_id,
        user_id=employee_user,
        route="POST /api/me/attendance/check-in",
        status=500,
        day=NOV4,
        **hit,
    )
    meter.record(company_id=other, user_id=None, route="GET /api/shifts", status=404, day=NOV4, **hit)
    meter.record(company_id=None, user_id=None, route="POST /api/auth/login", status=200, day=NOV4, **hit)
    meter.flush()


def test_platform_and_company_usage(client, admin_headers, company_headers, monkeypatch):
    set_today(monkeypatch, date(2026, 11, 20))
    company_id, user_id = _company_and_user()
    employee = create_employee(client, company_headers).json()["data"]
    other = company_with_plan(client, admin_headers)
    _traffic(company_id, user_id, employee["user_id"], other)
    with SessionLocal() as db:
        db.add(StorageSnapshot(company_id=company_id, day=NOV3, category="PEOPLE", rows=5, bytes=500))
        db.add(StorageSnapshot(company_id=company_id, day=NOV4, category="PEOPLE", rows=6, bytes=600))
        db.add(StorageSnapshot(company_id=company_id, day=NOV4, category="ATTENDANCE", rows=10, bytes=4000))
        db.commit()

    overview = client.get("/api/admin/usage/overview", headers=admin_headers).json()
    assert overview["code"] == "USAGE_OVERVIEW"
    data = overview["data"]
    assert (data["start"], data["end"], len(data["days"])) == ("2026-11-01", "2026-11-20", 20)
    assert data["totals"] == {
        "requests": 5,
        "bytes_in": 5 * 1024,
        "bytes_out": 5 * 4096,
        "duration_ms": 100,
        "avg_ms": 20.0,
        "server_errors": 1,
        "client_errors": 1,
    }
    assert data["days"][2]["requests"] == 1 and data["days"][3]["requests"] == 4 and data["days"][0]["requests"] == 0
    assert data["companies_with_traffic"] == 2
    assert data["storage"] == {
        "day": "2026-11-04",
        "rows": 16,
        "bytes": 4600,
        "items": data["storage"]["items"],
    }
    assert {i["category"]: i["bytes"] for i in data["storage"]["items"]} == {"PEOPLE": 600, "ATTENDANCE": 4000}

    companies = client.get("/api/admin/usage/companies", headers=admin_headers).json()
    assert companies["code"] == "USAGE_COMPANIES" and companies["data"]["total"] == 2
    top = companies["data"]["items"][0]
    assert top == {
        **top,
        "company_id": company_id,
        "requests": 3,
        "share": 60.0,
        "storage_bytes": 4600,
        "storage_rows": 16,
        "active_employees": 1,
        "status": "ACTIVE",
    }
    for sort in ("bytes", "duration", "errors", "storage"):
        listed = client.get(f"/api/admin/usage/companies?sort={sort}", headers=admin_headers).json()["data"]
        assert listed["items"][0]["company_id"] == company_id, sort
    searched = client.get("/api/admin/usage/companies?search=panif", headers=admin_headers).json()["data"]
    assert [r["company_id"] for r in searched["items"]] == [other] and searched["items"][0]["client_errors"] == 1
    assert client.get("/api/admin/usage/companies?sort=cost", headers=admin_headers).status_code == 422

    detail = client.get(
        f"/api/admin/usage/companies/{company_id}?start=2026-11-03&end=2026-11-04", headers=admin_headers
    ).json()
    assert detail["code"] == "COMPANY_USAGE"
    company = detail["data"]
    assert (company["totals"]["requests"], company["active_employees"], company["billing"]) == (3, 1, None)
    assert [r["route"] for r in company["top_routes"]] == ["GET /api/employees", "POST /api/me/attendance/check-in"]
    assert company["top_routes"][0] == {**company["top_routes"][0], "requests": 2, "max_ms": 20, "avg_ms": 20.0}
    assert company["storage"]["day"] == "2026-11-04" and len(company["days"]) == 2
    billed = client.get(f"/api/admin/usage/companies/{other}", headers=admin_headers).json()["data"]["billing"]
    assert billed == {**billed, "pricing_mode": "PER_USER", "unit_price": "300.00", "price_period": "MONTH"}
    assert (
        client.get(f"/api/admin/usage/companies/{other}", headers=admin_headers).json()["data"]["storage"]["day"]
        is None
    )

    users = client.get(f"/api/admin/usage/companies/{company_id}/users", headers=admin_headers).json()
    assert users["code"] == "USAGE_USERS" and users["data"]["total"] == 2
    first, second = users["data"]["items"]
    assert first == {
        **first,
        "user_id": user_id,
        "email": COMPANY_EMAIL,
        "role": "COMPANY",
        "name": None,
        "requests": 2,
    }
    assert second == {**second, "name": "Juan Pérez", "role": "EMPLOYEE", "server_errors": 1}
    assert round(first["share"] + second["share"], 1) == 100.0
    anonymous = client.get(f"/api/admin/usage/companies/{other}/users", headers=admin_headers).json()["data"]
    assert anonymous["items"][0] == {**anonymous["items"][0], "user_id": 0, "email": None}
    routes = client.get(f"/api/admin/usage/companies/{company_id}/routes?size=1", headers=admin_headers).json()
    assert routes["code"] == "USAGE_ROUTES" and routes["data"]["total"] == 2 and len(routes["data"]["items"]) == 1


def test_ranges_are_validated_and_companies_must_exist(client, admin_headers, monkeypatch):
    set_today(monkeypatch, date(2026, 11, 20))
    reversed_range = client.get("/api/admin/usage/overview?start=2026-11-10&end=2026-11-01", headers=admin_headers)
    assert reversed_range.status_code == 422 and reversed_range.json()["code"] == "INVALID_RANGE"
    too_long = client.get("/api/admin/usage/overview?start=2025-01-01&end=2026-11-01", headers=admin_headers)
    assert too_long.status_code == 422 and too_long.json()["code"] == "RANGE_TOO_LONG"
    empty = client.get("/api/admin/usage/overview?end=2026-11-02", headers=admin_headers).json()["data"]
    assert empty["totals"]["avg_ms"] == 0.0 and empty["storage"] == {"day": None, "rows": 0, "bytes": 0, "items": []}
    for url in ("", "/users", "/routes"):
        response = client.get(f"/api/admin/usage/companies/999{url}", headers=admin_headers)
        assert response.status_code == 404 and response.json()["code"] == "COMPANY_NOT_FOUND"
