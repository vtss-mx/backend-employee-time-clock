"""Turnos de trabajo: sitios con geocerca, turnos, su asignación (con un día de anticipación) y las
solicitudes de cambio de turno del empleado."""

from datetime import date, timedelta

import pytest

from app.core.clock import business_today
from app.core.database import SessionLocal
from app.models import Employee
from tests.conftest import create_company, login
from tests.test_validators import approved

SITES = "/api/sites"
SHIFTS = "/api/shifts"
POINT = (29.0729, -110.9559)  # Hermosillo


def address(latitude: float | None = POINT[0], longitude: float | None = POINT[1]) -> dict:
    return {
        "street": "Av. Reforma",
        "exterior_number": "100",
        "country_code": "MX",
        "postal_code": "83000",
        "state": "Sonora",
        "municipality": "Hermosillo",
        "city": "Hermosillo",
        "latitude": latitude,
        "longitude": longitude,
    }


def create_site(client, headers, name="Planta Norte", radius=100, point=POINT) -> dict:
    response = client.post(SITES, json={"name": name, "address": address(*point), "radius_m": radius}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def shift_body(name="Matutino", **changes) -> dict:
    return {
        "name": name,
        "start_time": "08:00",
        "end_time": "16:00",
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
        "breaks_count": 1,
        "break_minutes": 30,
        "early_check_in_minutes": 15,
        "late_tolerance_minutes": 10,
        "early_check_out_minutes": 5,
        "late_check_out_minutes": 60,
        **changes,
    }


def create_shift(client, headers, **changes) -> dict:
    response = client.post(SHIFTS, json=shift_body(**changes), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def assign(client, headers, employee_id: int, shift_id: int, valid_from: date, *, remote=(), sites=()):
    body = {
        "shift_id": shift_id,
        "valid_from": valid_from.isoformat(),
        "remote_weekdays": list(remote),
        "site_ids": list(sites),
    }
    return client.post(f"/api/employees/{employee_id}/shift-assignments", json=body, headers=headers)


def employee_with_face(client, company_headers, person="ana", number="EMP-001") -> tuple[int, dict]:
    """(id del empleado, headers de su sesión) con el rostro aprobado."""
    data = approved(client, company_headers, person, number=number)
    return data["id"], login(client, f"{person}@empresa.com", "Empleado123")


# ---------------------------------------------------------------- sitios


def test_empty_lists_before_anything_is_configured(client, company_headers):
    """Una empresa recién creada: sin sitios, turnos, solicitudes ni jornadas (y sin consultas de más)."""
    employee_id, headers = employee_with_face(client, company_headers)
    for url in (SITES, SHIFTS, "/api/shift-requests", f"/api/employees/{employee_id}/shift-assignments"):
        assert client.get(url, headers=company_headers).json()["data"]["total"] == 0, url
    board = client.get("/api/attendance/board", headers=company_headers).json()["data"]
    assert board["total"] == 0 and board["working"] == 0
    assert client.get("/api/attendance/sessions", headers=company_headers).json()["data"]["total"] == 0
    assert client.get("/api/me/attendance/history", headers=headers).json()["data"]["total"] == 0
    assert client.get("/api/me/shift-requests", headers=headers).json()["data"]["total"] == 0
    nothing = client.get("/api/me/attendance/today", headers=headers).json()["data"]
    assert nothing["actions"] == [] and nothing["message"] == "No tienes un turno asignado: pídeselo a tu empresa."


def test_sites_with_their_geofence(client, company_headers, admin_headers):
    site = create_site(client, company_headers)
    assert site["address"]["city"] == "Hermosillo" and site["radius_m"] == 100 and site["active"] is True
    duplicate = client.post(
        SITES, json={"name": "planta norte", "address": address(), "radius_m": 50}, headers=company_headers
    )
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "SITE_NAME_TAKEN"
    no_point = client.post(
        SITES, json={"name": "Sin punto", "address": address(None, None), "radius_m": 50}, headers=company_headers
    )
    assert no_point.status_code == 422
    url = f"{SITES}/{site['id']}"
    updated = client.put(url, json={"name": "Planta 1", "address": address(), "radius_m": 250}, headers=company_headers)
    assert updated.json()["data"]["name"] == "Planta 1" and updated.json()["data"]["radius_m"] == 250
    off = client.patch(f"{url}/status", json={"active": False}, headers=company_headers).json()["data"]
    assert off["active"] is False
    listed = client.get(SITES, params={"active": False, "search": "plan"}, headers=company_headers).json()["data"]
    assert [s["name"] for s in listed["items"]] == ["Planta 1"]
    assert client.get(SITES, headers=company_headers).json()["data"]["total"] == 1
    assert client.get(url, headers=company_headers).json()["data"]["employees"] == 0

    # Otra empresa no lo ve.
    assert create_company(client, admin_headers).status_code == 201
    other = login(client, "admin@panificadora.com", "Empresa1234")
    assert client.get(url, headers=other).status_code == 404
    assert client.delete(url, headers=company_headers).status_code == 200
    assert client.get(url, headers=company_headers).json()["code"] == "SITE_NOT_FOUND"


# ---------------------------------------------------------------- turnos


def test_shifts_validate_their_schedule(client, company_headers):
    shift = create_shift(client, company_headers)
    assert (
        shift["overnight"] is False and shift["duration_minutes"] == 480 and shift["weekdays"] == [0, 1, 2, 3, 4, 5, 6]
    )
    night = create_shift(client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00")
    assert night["overnight"] is True and night["duration_minutes"] == 480
    assert (
        client.post(SHIFTS, json=shift_body("matutino"), headers=company_headers).json()["code"] == "SHIFT_NAME_TAKEN"
    )
    for invalid, message in (
        ({"end_time": "08:00"}, "La hora de salida debe ser distinta de la de entrada"),
        ({"breaks_count": 1, "break_minutes": 3}, "Cada descanso dura al menos 5 minutos"),
        ({"breaks_count": 6, "break_minutes": 90}, "Los descansos no pueden sumar todo el turno"),
        (
            {"start_time": "06:00", "end_time": "05:00", "late_check_out_minutes": 120},
            "La entrada temprana, el turno y el límite de salida deben sumar menos de 24 horas",
        ),
        ({"weekdays": []}, None),
        ({"weekdays": [7]}, "Los días van de 0 (lunes) a 6 (domingo)"),
        ({"name": " x "}, "El nombre es obligatorio"),
    ):
        response = client.post(SHIFTS, json={**shift_body("Prueba"), **invalid}, headers=company_headers)
        assert response.status_code == 422 and response.json()["code"] == "VALIDATION_ERROR", invalid
        assert message is None or response.json()["message"] == message, response.json()["message"]
    no_breaks = create_shift(client, company_headers, name="Sin descanso", breaks_count=0, break_minutes=45)
    assert no_breaks["break_minutes"] == 0

    url = f"{SHIFTS}/{shift['id']}"
    edited = client.put(
        url, json=shift_body("Matutino 7-15", start_time="07:00", end_time="15:00"), headers=company_headers
    )
    assert edited.json()["data"]["name"] == "Matutino 7-15"
    assert (
        client.patch(f"{url}/status", json={"active": False}, headers=company_headers).json()["data"]["active"] is False
    )
    listed = client.get(SHIFTS, params={"active": True}, headers=company_headers).json()["data"]
    assert [s["name"] for s in listed["items"]] == ["Nocturno", "Sin descanso"]
    found = client.get(SHIFTS, params={"search": " matutino "}, headers=company_headers).json()["data"]
    assert [s["name"] for s in found["items"]] == ["Matutino 7-15"] and found["items"][0]["active"] is False
    assert client.get(url, headers=company_headers).json()["data"]["start_time"] == "07:00:00"
    assert client.delete(url, headers=company_headers).status_code == 200
    assert client.get(url, headers=company_headers).status_code == 404


# ---------------------------------------------------------------- asignaciones


def test_assignment_rules_and_changes_with_one_day_notice(client, company_headers):
    employee_id, _ = employee_with_face(client, company_headers)
    site = create_site(client, company_headers)
    morning = create_shift(client, company_headers)
    night = create_shift(client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00")
    today = business_today()

    for remote, sites, code in (
        ([7], [site["id"]], None),  # día inválido: lo rechaza el esquema
        ([], [], "SITE_REQUIRED"),  # sin sitio y ningún día remoto
        ([], [999], "SITE_NOT_AVAILABLE"),
    ):
        bad = assign(client, company_headers, employee_id, morning["id"], today, remote=remote, sites=sites)
        assert bad.status_code == 422 and (code is None or bad.json()["code"] == code)
    assert (
        assign(
            client, company_headers, employee_id, morning["id"], today - timedelta(days=1), sites=[site["id"]]
        ).json()["code"]
        == "ASSIGNMENT_IN_PAST"
    )

    # La primera asignación puede empezar hoy (todos los días remotos: no necesita sitio).
    first = assign(client, company_headers, employee_id, morning["id"], today, remote=range(7))
    assert first.status_code == 201 and first.json()["data"]["state"] == "CURRENT"
    # Con turno vigente, el cambio es desde mañana o después.
    same_day = assign(client, company_headers, employee_id, night["id"], today, sites=[site["id"]])
    assert same_day.status_code == 422 and same_day.json()["code"] == "ASSIGNMENT_NOTICE_REQUIRED"
    change = assign(client, company_headers, employee_id, night["id"], today + timedelta(days=2), sites=[site["id"]])
    assert change.status_code == 201 and change.json()["data"]["state"] == "SCHEDULED"
    assert change.json()["data"]["sites"][0]["name"] == "Planta Norte"
    clash = assign(client, company_headers, employee_id, morning["id"], today + timedelta(days=1), remote=range(7))
    assert clash.status_code == 409 and clash.json()["code"] == "ASSIGNMENT_ALREADY_SCHEDULED"

    history = client.get(f"/api/employees/{employee_id}/shift-assignments", headers=company_headers).json()["data"]
    assert [a["shift"]["name"] for a in history["items"]] == ["Nocturno", "Matutino"]
    assert history["items"][1]["valid_to"] == (today + timedelta(days=1)).isoformat()  # termina un día antes
    # El sitio en uso no se borra; el turno en uso tampoco.
    assert client.delete(f"{SITES}/{site['id']}", headers=company_headers).json()["code"] == "SITE_IN_USE"
    assert client.delete(f"{SHIFTS}/{night['id']}", headers=company_headers).json()["code"] == "SHIFT_IN_USE"
    assert client.get(f"{SHIFTS}/{morning['id']}", headers=company_headers).json()["data"]["employees"] == 1

    # Cancelar el cambio programado devuelve la vigencia a la asignación anterior.
    url = f"/api/shift-assignments/{change.json()['data']['id']}"
    assert client.delete(url, headers=company_headers).status_code == 200
    current = client.get(f"/api/employees/{employee_id}/shift-assignments", headers=company_headers).json()["data"]
    assert current["total"] == 1 and current["items"][0]["valid_to"] is None
    started = client.delete(f"/api/shift-assignments/{first.json()['data']['id']}", headers=company_headers)
    assert started.status_code == 409 and started.json()["code"] == "ASSIGNMENT_STARTED"
    assert client.delete("/api/shift-assignments/999", headers=company_headers).status_code == 404


def test_only_active_shifts_sites_and_employees(client, company_headers):
    employee_id, _ = employee_with_face(client, company_headers)
    site = create_site(client, company_headers)
    shift = create_shift(client, company_headers)
    today = business_today()
    client.patch(f"{SITES}/{site['id']}/status", json={"active": False}, headers=company_headers)
    inactive_site = assign(client, company_headers, employee_id, shift["id"], today, sites=[site["id"]])
    assert inactive_site.json()["code"] == "SITE_NOT_AVAILABLE"
    weekend = create_shift(client, company_headers, name="Fin de semana", weekdays=[5, 6])
    outside = assign(client, company_headers, employee_id, weekend["id"], today, remote=[0])
    assert outside.json()["code"] == "REMOTE_DAY_OUTSIDE_SHIFT" and "lunes" in outside.json()["message"]
    client.patch(f"{SHIFTS}/{shift['id']}/status", json={"active": False}, headers=company_headers)
    assert (
        assign(client, company_headers, employee_id, shift["id"], today, remote=range(7)).json()["code"]
        == "SHIFT_INACTIVE"
    )
    with SessionLocal() as db:
        db.get(Employee, employee_id).active = False  # type: ignore[union-attr]
        db.commit()
    assert (
        assign(client, company_headers, employee_id, weekend["id"], today, remote=[5]).json()["code"]
        == "EMPLOYEE_INACTIVE"
    )
    assert assign(client, company_headers, 999, weekend["id"], today).status_code == 404


# ---------------------------------------------------------------- solicitudes de cambio


@pytest.fixture
def requester(client, company_headers) -> dict:
    """Empleado con turno matutino remoto todos los días (para pedir el nocturno)."""
    employee_id, headers = employee_with_face(client, company_headers)
    site = create_site(client, company_headers)
    morning = create_shift(client, company_headers)
    night = create_shift(client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00")
    assert (
        assign(
            client, company_headers, employee_id, morning["id"], business_today(), remote=[0, 1], sites=[site["id"]]
        ).status_code
        == 201
    )
    return {"id": employee_id, "headers": headers, "night": night, "morning": morning, "site": site}


def test_employee_requests_a_shift_change_and_the_company_approves(client, company_headers, requester):
    headers, night = requester["headers"], requester["night"]
    shifts = client.get("/api/me/shifts", headers=headers).json()["data"]
    assert [s["name"] for s in shifts["items"]] == ["Matutino", "Nocturno"]
    tomorrow = business_today() + timedelta(days=1)

    too_soon = {"shift_id": night["id"], "valid_from": business_today().isoformat(), "reason": "Estudio en la mañana"}
    assert (
        client.post("/api/me/shift-requests", json=too_soon, headers=headers).json()["code"]
        == "SHIFT_REQUEST_NOTICE_REQUIRED"
    )
    body = {**too_soon, "valid_from": (tomorrow + timedelta(days=1)).isoformat()}
    created = client.post("/api/me/shift-requests", json=body, headers=headers)
    assert created.status_code == 201 and created.json()["data"]["current_shift"]["name"] == "Matutino"
    again = client.post("/api/me/shift-requests", json=body, headers=headers)
    assert again.status_code == 409 and again.json()["code"] == "SHIFT_REQUEST_PENDING"
    assert client.get("/api/shift-requests/summary", headers=company_headers).json()["data"] == {"pending": 1}

    inbox = client.get("/api/shift-requests", params={"status": "PENDING"}, headers=company_headers).json()["data"]
    request_id = inbox["items"][0]["id"]
    assert (
        inbox["items"][0]["employee"]["employee_number"] == "EMP-001"
        and inbox["items"][0]["reason"] == "Estudio en la mañana"
    )
    approved_request = client.post(f"/api/shift-requests/{request_id}/approve", json={}, headers=company_headers)
    assert approved_request.status_code == 200 and approved_request.json()["data"]["status"] == "APPROVED"
    assignments = client.get(f"/api/employees/{requester['id']}/shift-assignments", headers=company_headers).json()[
        "data"
    ]
    scheduled = assignments["items"][0]
    # Conserva los días remotos que el nuevo turno trabaja y los sitios de la asignación anterior.
    assert scheduled["shift"]["name"] == "Nocturno" and scheduled["state"] == "SCHEDULED"
    assert scheduled["remote_weekdays"] == [0, 1] and [s["name"] for s in scheduled["sites"]] == ["Planta Norte"]
    closed = client.post(
        f"/api/shift-requests/{request_id}/reject", json={"note": "Ya se aprobó"}, headers=company_headers
    )
    assert closed.status_code == 409 and closed.json()["code"] == "SHIFT_REQUEST_CLOSED"
    mine = client.get("/api/me/shift-requests", headers=headers).json()["data"]
    assert mine["items"][0]["status"] == "APPROVED"


def test_requests_can_be_cancelled_rejected_and_rescheduled(client, company_headers, requester):
    headers, night = requester["headers"], requester["night"]
    future = (business_today() + timedelta(days=3)).isoformat()
    request = client.post(
        "/api/me/shift-requests",
        json={"shift_id": night["id"], "valid_from": future, "reason": "  Cambio   de  horario "},
        headers=headers,
    ).json()["data"]
    assert request["reason"] == "Cambio de horario"
    cancelled = client.post(f"/api/me/shift-requests/{request['id']}/cancel", headers=headers).json()["data"]
    assert cancelled["status"] == "CANCELLED"
    assert (
        client.post(f"/api/me/shift-requests/{request['id']}/cancel", headers=headers).json()["code"]
        == "SHIFT_REQUEST_CLOSED"
    )

    second = client.post(
        "/api/me/shift-requests",
        json={"shift_id": night["id"], "valid_from": future, "reason": "Otra vez"},
        headers=headers,
    ).json()["data"]
    rejected = client.post(
        f"/api/shift-requests/{second['id']}/reject", json={"note": "No hay cupo en la noche"}, headers=company_headers
    )
    assert (
        rejected.json()["data"]["status"] == "REJECTED"
        and rejected.json()["data"]["review_note"] == "No hay cupo en la noche"
    )
    assert client.post("/api/shift-requests/999/approve", json={}, headers=company_headers).status_code == 404

    # La fecha pedida ya no tiene un día de anticipación: la empresa elige otra al aprobar.
    third = client.post(
        "/api/me/shift-requests",
        json={"shift_id": night["id"], "valid_from": future, "reason": "Tercera"},
        headers=headers,
    ).json()["data"]
    late = client.post(
        f"/api/shift-requests/{third['id']}/approve",
        json={"valid_from": business_today().isoformat()},
        headers=company_headers,
    )
    assert late.status_code == 422 and late.json()["code"] == "SHIFT_REQUEST_NOTICE_REQUIRED"
    other_day = (business_today() + timedelta(days=5)).isoformat()
    approved_request = client.post(
        f"/api/shift-requests/{third['id']}/approve",
        json={"valid_from": other_day, "remote_weekdays": list(range(7)), "site_ids": []},
        headers=company_headers,
    )
    assert approved_request.status_code == 200
    # Otro empleado no cancela solicitudes ajenas.
    _, stranger = employee_with_face(client, company_headers, person="beto", number="EMP-002")
    assert client.post(f"/api/me/shift-requests/{third['id']}/cancel", headers=stranger).status_code == 404


def test_requests_need_an_active_shift(client, company_headers, requester):
    morning, headers = requester["morning"], requester["headers"]
    client.patch(f"{SHIFTS}/{morning['id']}/status", json={"active": False}, headers=company_headers)
    future = (business_today() + timedelta(days=3)).isoformat()
    inactive = client.post(
        "/api/me/shift-requests",
        json={"shift_id": morning["id"], "valid_from": future, "reason": "Quiero este"},
        headers=headers,
    )
    assert inactive.status_code == 422 and inactive.json()["code"] == "SHIFT_INACTIVE"
    blank = client.post(
        "/api/me/shift-requests",
        json={"shift_id": requester["night"]["id"], "valid_from": future, "reason": "a    b"},
        headers=headers,
    )
    assert blank.status_code == 422 and blank.json()["message"] == "Explica brevemente el motivo"
    assert (
        client.post(
            "/api/me/shift-requests",
            json={"shift_id": 999, "valid_from": future, "reason": "No existe"},
            headers=headers,
        ).status_code
        == 404
    )


def test_two_simultaneous_requests_leave_one_pending(client, company_headers, requester, monkeypatch):
    """Si dos solicitudes pasan la revisión a la vez, el índice único deja una y la otra responde 409."""
    from app.repositories.shift_repository import ShiftRepository

    body = {
        "shift_id": requester["night"]["id"],
        "valid_from": (business_today() + timedelta(days=3)).isoformat(),
        "reason": "Cambio de horario",
    }
    headers = requester["headers"]
    assert client.post("/api/me/shift-requests", json=body, headers=headers).status_code == 201
    monkeypatch.setattr(ShiftRepository, "has_pending_request", lambda self, employee_id: False)
    raced = client.post("/api/me/shift-requests", json=body, headers=headers)
    assert raced.status_code == 409 and raced.json()["code"] == "SHIFT_REQUEST_PENDING"
    assert client.get("/api/me/shift-requests", headers=headers).json()["data"]["total"] == 1
