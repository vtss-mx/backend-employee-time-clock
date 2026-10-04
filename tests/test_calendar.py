"""Calendario de días libres por la API: festivos, ausencias (también colectivas), solicitudes del
empleado y días laborables especiales."""

from datetime import date, timedelta

import pytest
from sqlalchemy import insert

from app.core.clock import business_today
from app.core.database import SessionLocal
from app.models import CatalogDayOffType, Employee, EmployeeAbsence
from app.services.calendar_rules import official_holidays
from app.services.catalog_service import clear_catalog_cache, get_catalogs
from tests.conftest import create_company, create_employee, login
from tests.test_attendance import server_clock
from tests.test_shifts import employee_with_face

CAL = "/api/calendar"
TODAY = business_today()


@pytest.fixture
def clock(monkeypatch):
    return server_clock(monkeypatch)


def person(client, headers, number: str, email: str) -> int:
    created = create_employee(client, headers, number=number, email=email)
    assert created.status_code == 201, created.text
    return created.json()["data"]["id"]


@pytest.fixture
def staff(client, company_headers) -> list[int]:
    """Tres empleados (Juan, Ana y Beto) sin rostro: la empresa los administra."""
    return [
        person(client, company_headers, "EMP-001", "juan@empresa.com"),
        person(client, company_headers, "EMP-002", "ana2@empresa.com"),
        person(client, company_headers, "EMP-003", "beto2@empresa.com"),
    ]


def absence_body(employee_ids, *, kind="VACATION", start=None, days=5, note=None) -> dict:
    start = start or TODAY + timedelta(days=10)
    return {
        "employee_ids": list(employee_ids),
        "type": kind,
        "starts_on": start.isoformat(),
        "ends_on": (start + timedelta(days=days - 1)).isoformat(),
        "note": note,
    }


# ---------------------------------------------------------------- festivos


def test_holidays_by_year_with_the_official_ones(client, company_headers, admin_headers):
    year = TODAY.year + 1
    empty = client.get(f"{CAL}/holidays", params={"year": year}, headers=company_headers).json()["data"]
    assert empty["total"] == 0 and empty["items"] == []
    own = client.post(
        f"{CAL}/holidays",
        json={"holiday_date": f"{year}-12-25", "name": " Posada  de la empresa "},
        headers=company_headers,
    )
    assert own.status_code == 201 and own.json()["data"]["name"] == "Posada de la empresa"
    assert own.json()["data"]["official"] is False
    official = client.post(f"{CAL}/holidays/official", params={"year": year}, headers=company_headers)
    assert official.status_code == 200 and official.json()["code"] == "OFFICIAL_HOLIDAYS_ADDED"
    added = official.json()["data"]
    # El 25 de diciembre ya era festivo (con otro nombre): se respeta y no se duplica.
    assert added["existing"] == 1 and len(added["added"]) == len(official_holidays(year)) - 1 and added["year"] == year
    assert all(h["official"] for h in added["added"]) and f"{year}-12-25" not in [
        h["holiday_date"] for h in added["added"]
    ]
    again = client.post(f"{CAL}/holidays/official", params={"year": year}, headers=company_headers).json()["data"]
    assert again["added"] == [] and again["existing"] == len(added["added"]) + 1  # idempotente
    page = client.get(f"{CAL}/holidays", params={"year": year, "size": 3}, headers=company_headers).json()["data"]
    assert page["total"] == len(added["added"]) + 1 and len(page["items"]) == 3
    assert [h["holiday_date"] for h in page["items"]] == sorted(h["holiday_date"] for h in page["items"])
    assert page["items"][0]["name"] == "Año Nuevo"

    duplicate = client.post(
        f"{CAL}/holidays", json={"holiday_date": f"{year}-01-01", "name": "Otro"}, headers=company_headers
    )
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "HOLIDAY_DATE_TAKEN"
    blank = client.post(
        f"{CAL}/holidays", json={"holiday_date": f"{year}-06-01", "name": " x "}, headers=company_headers
    )
    assert blank.status_code == 422
    out_of_range = client.get(f"{CAL}/holidays", params={"year": 1999}, headers=company_headers)
    assert out_of_range.status_code == 422

    # Otra empresa no ve ni borra los festivos de esta.
    holiday_id = own.json()["data"]["id"]
    assert create_company(client, admin_headers).status_code == 201
    other = login(client, "admin@panificadora.com", "Empresa1234")
    assert client.get(f"{CAL}/holidays", params={"year": year}, headers=other).json()["data"]["total"] == 0
    assert client.delete(f"{CAL}/holidays/{holiday_id}", headers=other).json()["code"] == "HOLIDAY_NOT_FOUND"
    assert client.delete(f"{CAL}/holidays/{holiday_id}", headers=company_headers).status_code == 200
    assert client.delete(f"{CAL}/holidays/{holiday_id}", headers=company_headers).status_code == 404


def test_two_holidays_on_the_same_day_at_the_same_time(client, company_headers, monkeypatch):
    """Si dos altas pasan la revisión a la vez, la restricción única deja una y la otra responde 409."""
    from app.repositories.calendar_repository import CalendarRepository

    body = {"holiday_date": f"{TODAY.year + 1}-07-01", "name": "Aniversario"}
    assert client.post(f"{CAL}/holidays", json=body, headers=company_headers).status_code == 201
    monkeypatch.setattr(CalendarRepository, "holiday_names", lambda self, start, end: {})
    raced = client.post(f"{CAL}/holidays", json=body, headers=company_headers)
    assert raced.status_code == 409 and raced.json()["code"] == "HOLIDAY_DATE_TAKEN"


# ---------------------------------------------------------------- ausencias de la empresa


def test_collective_vacation_for_several_employees(client, company_headers, staff):
    juan, ana, beto = staff
    with SessionLocal() as db:
        db.get(Employee, beto).active = False  # type: ignore[union-attr]
        db.commit()
    body = absence_body(staff, note="  Vacaciones   de fin de año ")
    created = client.post(f"{CAL}/absences", json=body, headers=company_headers)
    assert created.status_code == 200 and created.json()["code"] == "ABSENCES_CREATED"
    result = created.json()["data"]
    assert (result["done"], result["unchanged"], result["skipped"]) == (2, 0, 1)
    skipped = next(r for r in result["results"] if r["result"] == "SKIPPED")
    assert skipped["employee"]["id"] == beto and skipped["code"] == "EMPLOYEE_INACTIVE"
    # Un reintento no duplica: quienes ya la tienen quedan "sin cambios".
    retry = client.post(f"{CAL}/absences", json=body, headers=company_headers).json()["data"]
    assert (retry["done"], retry["unchanged"], retry["skipped"]) == (0, 2, 1)
    # Otra que se encima se omite con su motivo.
    clash = client.post(
        f"{CAL}/absences",
        json=absence_body([juan], kind="PERMISSION", start=TODAY + timedelta(days=12), days=1),
        headers=company_headers,
    ).json()["data"]
    assert clash["skipped"] == 1 and clash["results"][0]["code"] == "ABSENCE_OVERLAP"
    assert "«Vacaciones» (aprobada)" in clash["results"][0]["message"]

    listed = client.get(f"{CAL}/absences", headers=company_headers).json()["data"]
    assert listed["total"] == 2
    item = listed["items"][0]
    assert item["status"] == "APPROVED" and item["days"] == 5 and item["note"] == "Vacaciones de fin de año"
    assert item["requested_by_employee"] is False and item["decided_at"] is not None
    filters = {"employee_id": ana, "type": "VACATION", "status": "APPROVED"}
    assert client.get(f"{CAL}/absences", params=filters, headers=company_headers).json()["data"]["total"] == 1
    window = {"start": (TODAY + timedelta(days=14)).isoformat(), "end": (TODAY + timedelta(days=30)).isoformat()}
    assert client.get(f"{CAL}/absences", params=window, headers=company_headers).json()["data"]["total"] == 2
    before = {"end": (TODAY + timedelta(days=9)).isoformat()}
    assert client.get(f"{CAL}/absences", params=before, headers=company_headers).json()["data"]["total"] == 0
    after = {"start": (TODAY + timedelta(days=15)).isoformat()}
    assert client.get(f"{CAL}/absences", params=after, headers=company_headers).json()["data"]["total"] == 0

    # La empresa retira una ausencia; ya retirada no se vuelve a retirar.
    cancelled = client.post(f"{CAL}/absences/{item['id']}/cancel", headers=company_headers)
    assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "CANCELLED"
    again = client.post(f"{CAL}/absences/{item['id']}/cancel", headers=company_headers)
    assert again.status_code == 409 and again.json()["code"] == "ABSENCE_CLOSED"


def test_absence_requests_are_validated(client, company_headers, staff, admin_headers):
    juan = staff[0]
    for body, status in (
        (absence_body([juan], kind="NOPE"), 422),
        (absence_body([juan, juan]), 422),  # el mismo empleado dos veces
        (absence_body([]), 422),
        ({**absence_body([juan]), "ends_on": TODAY.isoformat()}, 422),  # termina antes de empezar
        (absence_body([juan], days=367), 422),  # más de un año
        (absence_body([999]), 404),
    ):
        response = client.post(f"{CAL}/absences", json=body, headers=company_headers)
        assert response.status_code == status, (body, response.text)
    assert client.post(f"{CAL}/absences", json=absence_body([999]), headers=company_headers).json()["errors"][0][
        "details"
    ] == {"employee_ids": [999]}
    assert (
        client.post(f"{CAL}/absences", json=absence_body([juan], kind="NOPE"), headers=company_headers).json()["code"]
        == "DAY_OFF_TYPE_INVALID"
    )
    # Un año completo (366 días) sí cabe.
    assert (
        client.post(f"{CAL}/absences", json=absence_body([juan], days=366), headers=company_headers).json()["data"][
            "done"
        ]
        == 1
    )
    # Un empleado de otra empresa responde 404 (como si no existiera) y no se registra nada.
    assert create_company(client, admin_headers).status_code == 201
    other = login(client, "admin@panificadora.com", "Empresa1234")
    foreign = client.post(f"{CAL}/absences", json=absence_body(staff[1:]), headers=other)
    assert foreign.status_code == 404 and foreign.json()["code"] == "EMPLOYEE_NOT_FOUND"
    assert client.get(f"{CAL}/absences", headers=company_headers).json()["data"]["total"] == 1
    for url in ("absences/1/approve", "absences/1/cancel", "absences/999/approve"):
        assert client.post(f"{CAL}/{url}", headers=other).status_code == 404, url


# ---------------------------------------------------------------- solicitudes del empleado


@pytest.fixture
def requester(client, company_headers) -> dict:
    employee_id, headers = employee_with_face(client, company_headers)
    return {"id": employee_id, "headers": headers}


def request_body(*, kind="VACATION", start=None, days=3, note="Viaje familiar") -> dict:
    start = start or TODAY + timedelta(days=20)
    return {
        "type": kind,
        "starts_on": start.isoformat(),
        "ends_on": (start + timedelta(days=days - 1)).isoformat(),
        "note": note,
    }


def test_employee_requests_vacation_and_the_company_approves(client, company_headers, requester):
    headers = requester["headers"]
    assert client.get("/api/me/absences", headers=headers).json()["data"]["total"] == 0
    assert client.get(f"{CAL}/absences/summary", headers=company_headers).json()["data"] == {"pending": 0}
    sick = client.post("/api/me/absences", json=request_body(kind="SICK_LEAVE"), headers=headers)
    assert sick.status_code == 422 and sick.json()["code"] == "DAY_OFF_TYPE_NOT_REQUESTABLE"
    assert "Incapacidad" in sick.json()["message"]
    past = client.post("/api/me/absences", json=request_body(start=TODAY - timedelta(days=1)), headers=headers)
    assert past.status_code == 422 and past.json()["code"] == "ABSENCE_IN_PAST"

    created = client.post("/api/me/absences", json=request_body(), headers=headers)
    assert created.status_code == 201 and created.json()["code"] == "ABSENCE_REQUESTED"
    request = created.json()["data"]
    assert request["status"] == "PENDING" and request["requested_by_employee"] is True and request["days"] == 3
    overlapping = client.post("/api/me/absences", json=request_body(kind="PERMISSION", days=1), headers=headers)
    assert overlapping.status_code == 409 and overlapping.json()["code"] == "ABSENCE_OVERLAP"
    assert "(pendiente)" in overlapping.json()["message"]
    assert client.get(f"{CAL}/absences/summary", headers=company_headers).json()["data"] == {"pending": 1}

    approved = client.post(f"{CAL}/absences/{request['id']}/approve", headers=company_headers)
    assert approved.status_code == 200 and approved.json()["data"]["status"] == "APPROVED"
    closed = client.post(
        f"{CAL}/absences/{request['id']}/reject", json={"note": "Ya se aprobó"}, headers=company_headers
    )
    assert closed.status_code == 409 and closed.json()["code"] == "ABSENCE_CLOSED"
    mine = client.get("/api/me/absences", headers=headers).json()["data"]
    assert mine["items"][0]["status"] == "APPROVED"
    cannot = client.post(f"/api/me/absences/{request['id']}/cancel", headers=headers)
    assert cannot.status_code == 409  # ya aprobada: la retira la empresa


def test_requests_can_be_rejected_cancelled_and_not_approved_over_another(client, company_headers, requester):
    headers = requester["headers"]
    first = client.post("/api/me/absences", json=request_body(note=None), headers=headers).json()["data"]
    assert first["note"] is None
    rejected = client.post(
        f"{CAL}/absences/{first['id']}/reject", json={"note": "  Hay   inventario "}, headers=company_headers
    )
    assert (
        rejected.json()["data"]["status"] == "REJECTED" and rejected.json()["data"]["decision_note"] == "Hay inventario"
    )
    short = client.post(f"{CAL}/absences/{first['id']}/reject", json={"note": "no"}, headers=company_headers)
    assert short.status_code == 422

    second = client.post("/api/me/absences", json=request_body(kind="PERMISSION", days=1), headers=headers).json()[
        "data"
    ]
    cancelled = client.post(f"/api/me/absences/{second['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "CANCELLED"

    # La empresa registró vacaciones aprobadas que se enciman con una solicitud pendiente creada antes.
    third = client.post("/api/me/absences", json=request_body(days=2), headers=headers).json()["data"]
    with SessionLocal() as db:
        db.execute(
            insert(EmployeeAbsence).values(
                company_id=db.get(Employee, requester["id"]).company_id,  # type: ignore[union-attr]
                employee_id=requester["id"],
                type_code="VACATION",
                starts_on=TODAY + timedelta(days=21),
                ends_on=TODAY + timedelta(days=21),
                status="APPROVED",
            )
        )
        db.commit()
    clash = client.post(f"{CAL}/absences/{third['id']}/approve", headers=company_headers)
    assert clash.status_code == 409 and clash.json()["code"] == "ABSENCE_OVERLAP"

    # Otro empleado no cancela solicitudes ajenas.
    _, stranger = employee_with_face(client, company_headers, person="beto", number="EMP-002")
    assert client.post(f"/api/me/absences/{third['id']}/cancel", headers=stranger).json()["code"] == "ABSENCE_NOT_FOUND"
    assert client.post("/api/me/absences/999/cancel", headers=stranger).status_code == 404


def test_employee_sees_the_upcoming_holidays(client, company_headers, requester):
    headers = requester["headers"]
    assert client.get("/api/me/holidays", headers=headers).json()["data"]["total"] == 0
    for day, name in ((TODAY - timedelta(days=1), "Ayer"), (TODAY, "Hoy"), (TODAY + timedelta(days=30), "Pronto")):
        client.post(f"{CAL}/holidays", json={"holiday_date": day.isoformat(), "name": name}, headers=company_headers)
    upcoming = client.get("/api/me/holidays", headers=headers).json()["data"]
    assert [h["name"] for h in upcoming["items"]] == ["Hoy", "Pronto"]


def test_an_absence_type_missing_from_the_cached_catalog_still_reads(client, company_headers, requester, clock):
    """Un tipo nuevo en la base que la caché de catálogos aún no tiene: se nombra con su código."""
    from tests.test_attendance import today as attendance_today
    from tests.test_shifts import assign, create_shift

    get_catalogs()  # caché cargada antes del tipo nuevo
    with SessionLocal() as db:
        db.execute(insert(CatalogDayOffType).values(code="STUDY", name="Estudio", phrase="Estás de estudio"))
        company_id = db.get(Employee, requester["id"]).company_id  # type: ignore[union-attr]
        day = TODAY + timedelta(days=3)
        db.execute(
            insert(EmployeeAbsence).values(
                company_id=company_id,
                employee_id=requester["id"],
                type_code="STUDY",
                starts_on=day,
                ends_on=day,
                status="APPROVED",
            )
        )
        db.commit()
    shift = create_shift(client, company_headers)
    assert assign(client, company_headers, requester["id"], shift["id"], TODAY, remote=range(7)).status_code == 201
    clock(day, "08:00")
    off = attendance_today(client, requester["headers"])
    assert off["day_off"]["kind"] == "STUDY" and off["day_off"]["name"] == "STUDY"
    assert off["message"].startswith(f"Tienes día libre el {day:%d/%m/%Y}")
    clear_catalog_cache()


# ---------------------------------------------------------------- días laborables especiales


def test_workdays_only_on_days_off(client, company_headers, staff, admin_headers):
    juan = staff[0]
    christmas = date(TODAY.year + 1, 12, 25)
    body = {"employee_id": juan, "work_date": christmas.isoformat(), "note": "Guardia"}
    not_needed = client.post(f"{CAL}/workdays", json=body, headers=company_headers)
    assert not_needed.status_code == 422 and not_needed.json()["code"] == "WORKDAY_NOT_NEEDED"
    client.post(
        f"{CAL}/holidays", json={"holiday_date": christmas.isoformat(), "name": "Navidad"}, headers=company_headers
    )
    created = client.post(f"{CAL}/workdays", json=body, headers=company_headers)
    assert created.status_code == 201 and created.json()["data"]["employee"]["id"] == juan
    assert created.json()["data"]["note"] == "Guardia"
    again = client.post(f"{CAL}/workdays", json=body, headers=company_headers)
    assert again.json()["code"] == "WORKDAY_NOT_NEEDED"  # ya es laborable para él
    # Dentro de unas vacaciones aprobadas también.
    client.post(f"{CAL}/absences", json=absence_body([juan], start=TODAY + timedelta(days=2)), headers=company_headers)
    inside = {"employee_id": juan, "work_date": (TODAY + timedelta(days=3)).isoformat()}
    assert client.post(f"{CAL}/workdays", json=inside, headers=company_headers).status_code == 201
    past = client.post(
        f"{CAL}/workdays",
        json={**inside, "work_date": (TODAY - timedelta(days=1)).isoformat()},
        headers=company_headers,
    )
    assert past.status_code == 422 and past.json()["code"] == "WORKDAY_IN_PAST"
    missing = client.post(f"{CAL}/workdays", json={**inside, "employee_id": 999}, headers=company_headers)
    assert missing.status_code == 404

    listed = client.get(f"{CAL}/workdays", params={"employee_id": juan}, headers=company_headers).json()["data"]
    assert listed["total"] == 2 and listed["items"][0]["work_date"] == christmas.isoformat()  # la más reciente primero
    ranged = {"start": TODAY.isoformat(), "end": (TODAY + timedelta(days=5)).isoformat()}
    assert client.get(f"{CAL}/workdays", params=ranged, headers=company_headers).json()["data"]["total"] == 1

    assert create_company(client, admin_headers).status_code == 201
    other = login(client, "admin@panificadora.com", "Empresa1234")
    workday_id = created.json()["data"]["id"]
    assert client.delete(f"{CAL}/workdays/{workday_id}", headers=other).json()["code"] == "WORKDAY_NOT_FOUND"
    assert client.delete(f"{CAL}/workdays/{workday_id}", headers=company_headers).status_code == 200
    assert client.get(f"{CAL}/workdays", headers=company_headers).json()["data"]["total"] == 1


def test_the_same_workday_marked_twice_at_once(client, company_headers, staff, monkeypatch):
    from app.services.calendar_rules import HOLIDAY, DayOff, DaysOff
    from app.services.calendar_service import CalendarService

    day = TODAY + timedelta(days=4)
    body = {"employee_id": staff[0], "work_date": day.isoformat()}
    christmas = DayOff(HOLIDAY, "Festivo", day, day)
    monkeypatch.setattr(
        CalendarService, "days_off", lambda self, ids, start, end: DaysOff(holidays={day: christmas.name})
    )
    assert client.post(f"{CAL}/workdays", json=body, headers=company_headers).status_code == 201
    raced = client.post(f"{CAL}/workdays", json=body, headers=company_headers)
    assert raced.status_code == 409 and raced.json()["code"] == "WORKDAY_TAKEN"


def test_catalogs_include_the_day_off_types(client, company_headers):
    data = client.get("/api/catalogs", headers=company_headers).json()["data"]
    types = {t["code"]: t for t in data["day_off_types"]}
    assert set(types) == {"VACATION", "PERMISSION", "SICK_LEAVE", "OTHER"}
    assert types["VACATION"]["requestable"] and types["PERMISSION"]["requestable"]
    assert not types["SICK_LEAVE"]["requestable"] and types["VACATION"]["phrase"] == "Estás de vacaciones"
    assert data["attendance_edit_reasons"][0]["code"] == "FORGOT"
    assert {s["code"] for s in data["board_states"]} >= {"DAY_OFF"}
