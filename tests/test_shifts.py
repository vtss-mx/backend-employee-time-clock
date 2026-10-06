"""Turnos de trabajo: sitios con geocerca, turnos (dónde y cuándo se checa), su asignación (solo el turno
y desde cuándo, con un día de anticipación) y las solicitudes de cambio de turno del empleado."""

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
        "neighborhood": "Centro",
        "latitude": latitude,
        "longitude": longitude,
    }


def create_site(client, headers, name="Planta Norte", radius=100, point=POINT) -> dict:
    response = client.post(SITES, json={"name": name, "address": address(*point), "radius_m": radius}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def shift_body(name="Matutino", *, sites=(), remote=None, **changes) -> dict:
    """Un turno de prueba. Dónde se checa: sin sitios, todos sus días son remotos (no necesita sitio); con
    sitios, se checa en ellos (salvo los días `remote`)."""
    body = {
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
    body["site_ids"] = list(sites)
    body["remote_weekdays"] = list(remote if remote is not None else [] if sites else body["weekdays"])
    return body


def create_shift(client, headers, **changes) -> dict:
    response = client.post(SHIFTS, json=shift_body(**changes), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def assign(client, headers, employee_id: int, shift_id: int, valid_from: date):
    """Asignar es solo elegir el turno y desde cuándo (dónde checa lo dice el turno)."""
    body = {"shift_id": shift_id, "valid_from": valid_from.isoformat()}
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
    assert client.get(url, headers=company_headers).json()["data"]["deleted_at"]  # en «Eliminados»
    edited = client.patch(f"{url}/status", json={"active": True}, headers=company_headers)
    assert edited.json()["code"] == "SITE_NOT_FOUND"


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
        ({"breaks_count": 1, "break_minutes": 3}, "Cada descanso debe durar al menos 5 minutos"),
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
    assert client.get(url, headers=company_headers).json()["data"]["deleted_at"]  # en «Eliminados»


def test_the_shift_says_where_to_check_in(client, company_headers):
    """El turno dice dónde se checa: sus sitios (los nuevos, activos) y sus días remotos (días del turno);
    si algún día no es remoto, necesita al menos un sitio."""
    north = create_site(client, company_headers)
    south = create_site(client, company_headers, name="Planta Sur")
    closed = create_site(client, company_headers, name="Bodega")
    client.patch(f"{SITES}/{closed['id']}/status", json={"active": False}, headers=company_headers)
    workweek = {"weekdays": [0, 1, 2, 3, 4]}
    for changes, code in (
        ({"remote": []}, "SITE_REQUIRED"),  # ningún día remoto y ningún sitio
        ({"remote": [0, 1]}, "SITE_REQUIRED"),  # algunos días en sitio
        ({"sites": [north["id"]], "remote": [5]}, "REMOTE_DAY_OUTSIDE_SHIFT"),
        ({"sites": [999]}, "SITE_NOT_AVAILABLE"),
        ({"sites": [closed["id"]]}, "SITE_NOT_AVAILABLE"),
    ):
        response = client.post(SHIFTS, json=shift_body("Oficina", **workweek, **changes), headers=company_headers)
        assert response.status_code == 422 and response.json()["code"] == code, changes
    outside = client.post(SHIFTS, json=shift_body("Oficina", **workweek, remote=[5, 6]), headers=company_headers)
    assert outside.json()["message"] == "El turno no trabaja el sábado y domingo: no puede ser día remoto"
    assert outside.json()["errors"][0]["field"] == "remote_weekdays"
    for invalid in ({"site_ids": [0]}, {"site_ids": list(range(1, 52))}):
        bad = client.post(SHIFTS, json={**shift_body("Oficina"), **invalid}, headers=company_headers)
        assert bad.status_code == 422 and bad.json()["code"] == "VALIDATION_ERROR", invalid

    shift = create_shift(
        client, company_headers, name="Oficina", sites=[south["id"], north["id"], south["id"]], remote=[0], **workweek
    )
    assert [site["name"] for site in shift["sites"]] == ["Planta Norte", "Planta Sur"] and shift["remote_weekdays"] == [
        0
    ]
    first = shift["sites"][0]
    assert first["address"]["city"] == "Hermosillo" and first["radius_m"] == 100 and first["active"] is True
    listed = client.get(SHIFTS, headers=company_headers).json()["data"]["items"][0]
    assert listed["sites"] == shift["sites"] and listed["remote_weekdays"] == [0]

    # Un sitio que el turno ya tenía puede quedarse aunque se desactive (no acepta registros); uno nuevo no.
    client.patch(f"{SITES}/{north['id']}/status", json={"active": False}, headers=company_headers)
    url = f"{SHIFTS}/{shift['id']}"
    kept = client.put(
        url, json=shift_body("Oficina", sites=[north["id"], south["id"]], **workweek), headers=company_headers
    )
    assert kept.status_code == 200 and [s["active"] for s in kept.json()["data"]["sites"]] == [False, True]
    added = client.put(
        url, json=shift_body("Oficina", sites=[closed["id"], south["id"]], **workweek), headers=company_headers
    )
    assert added.json()["code"] == "SITE_NOT_AVAILABLE"
    moved = client.put(url, json=shift_body("Oficina", sites=[south["id"]], **workweek), headers=company_headers)
    assert [s["name"] for s in moved.json()["data"]["sites"]] == ["Planta Sur"]
    remote = client.put(url, json=shift_body("Oficina", **workweek), headers=company_headers).json()["data"]
    assert remote["sites"] == [] and remote["remote_weekdays"] == [0, 1, 2, 3, 4]
    assert client.get(url, headers=company_headers).json()["data"]["sites"] == []


def test_a_site_used_by_shifts_is_not_deleted(client, company_headers):
    """Borrar un sitio que algún turno usa responde 409 con los turnos que lo usan."""
    site = create_site(client, company_headers)
    url = f"{SITES}/{site['id']}"
    create_shift(client, company_headers, sites=[site["id"]])
    one = client.delete(url, headers=company_headers)
    assert one.status_code == 409 and one.json()["code"] == "SITE_IN_USE"
    assert one.json()["message"] == (
        "El sitio está en el turno Matutino: quítalo de ese turno o desactívalo en lugar de eliminarlo"
    )
    assert one.json()["errors"][0]["details"] == {"shifts": ["Matutino"]}
    night = create_shift(client, company_headers, name="Nocturno", sites=[site["id"]])
    two = client.delete(url, headers=company_headers).json()
    assert two["message"].startswith("El sitio está en los turnos Matutino y Nocturno: quítalo de esos turnos")
    for name in ("A", "B", "C", "D"):
        create_shift(client, company_headers, name=f"Turno {name}", sites=[site["id"]])
    many = client.delete(url, headers=company_headers).json()
    assert "Matutino, Nocturno, Turno A, Turno B, Turno C y otros:" in many["message"]
    assert len(many["errors"][0]["details"]["shifts"]) == 5
    # Sin turnos que lo usen (y sin registros) ya se puede eliminar.
    for shift in client.get(SHIFTS, headers=company_headers).json()["data"]["items"]:
        client.delete(f"{SHIFTS}/{shift['id']}", headers=company_headers)
    assert client.get(f"{SHIFTS}/{night['id']}", headers=company_headers).json()["data"]["deleted"] is True
    assert client.delete(url, headers=company_headers).status_code == 200


# ---------------------------------------------------------------- asignaciones


def test_assignment_rules_and_changes_with_one_day_notice(client, company_headers):
    employee_id, _ = employee_with_face(client, company_headers)
    site = create_site(client, company_headers)
    morning = create_shift(client, company_headers)
    night = create_shift(
        client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00", sites=[site["id"]]
    )
    today = business_today()

    past = assign(client, company_headers, employee_id, morning["id"], today - timedelta(days=1))
    assert past.json()["code"] == "ASSIGNMENT_IN_PAST"
    # Asignar es solo el turno y la fecha: lo demás que llegue no cuenta (el lugar es el del turno).
    legacy = client.post(
        f"/api/employees/{employee_id}/shift-assignments",
        json={
            "shift_id": morning["id"],
            "valid_from": today.isoformat(),
            "site_ids": [site["id"]],
            "remote_weekdays": [],
        },
        headers=company_headers,
    )
    # La primera asignación puede empezar hoy y dice dónde checa: lo de su turno (todos los días remotos).
    assert legacy.status_code == 201 and legacy.json()["data"]["state"] == "CURRENT"
    assert (
        legacy.json()["data"]["shift"]["remote_weekdays"] == list(range(7))
        and legacy.json()["data"]["shift"]["sites"] == []
    )
    assert "remote_weekdays" not in legacy.json()["data"] and "sites" not in legacy.json()["data"]
    first = assign(client, company_headers, employee_id, morning["id"], today)  # un reintento: la misma
    assert first.status_code == 201 and first.json()["data"]["id"] == legacy.json()["data"]["id"]
    # Con turno vigente, el cambio es desde mañana o después.
    same_day = assign(client, company_headers, employee_id, night["id"], today)
    assert same_day.status_code == 422 and same_day.json()["code"] == "ASSIGNMENT_NOTICE_REQUIRED"
    change = assign(client, company_headers, employee_id, night["id"], today + timedelta(days=2))
    assert change.status_code == 201 and change.json()["data"]["state"] == "SCHEDULED"
    assert change.json()["data"]["shift"]["sites"][0]["name"] == "Planta Norte"
    clash = assign(client, company_headers, employee_id, morning["id"], today + timedelta(days=1))
    assert clash.status_code == 409 and clash.json()["code"] == "ASSIGNMENT_ALREADY_SCHEDULED"

    history = client.get(f"/api/employees/{employee_id}/shift-assignments", headers=company_headers).json()["data"]
    assert [a["shift"]["name"] for a in history["items"]] == ["Nocturno", "Matutino"]
    assert history["items"][1]["valid_to"] == (today + timedelta(days=1)).isoformat()  # termina un día antes
    # El sitio en uso no se borra; el turno en uso tampoco. El sitio cuenta a quien hoy checa ahí.
    assert client.delete(f"{SITES}/{site['id']}", headers=company_headers).json()["code"] == "SITE_IN_USE"
    assert client.delete(f"{SHIFTS}/{night['id']}", headers=company_headers).json()["code"] == "SHIFT_IN_USE"
    assert client.get(f"{SHIFTS}/{morning['id']}", headers=company_headers).json()["data"]["employees"] == 1
    assert client.get(f"{SITES}/{site['id']}", headers=company_headers).json()["data"]["employees"] == 0

    # Cancelar el cambio programado devuelve la vigencia a la asignación anterior.
    url = f"/api/shift-assignments/{change.json()['data']['id']}"
    assert client.delete(url, headers=company_headers).status_code == 200
    current = client.get(f"/api/employees/{employee_id}/shift-assignments", headers=company_headers).json()["data"]
    assert current["total"] == 1 and current["items"][0]["valid_to"] is None
    started = client.delete(f"/api/shift-assignments/{first.json()['data']['id']}", headers=company_headers)
    assert started.status_code == 409 and started.json()["code"] == "ASSIGNMENT_STARTED"
    assert client.delete("/api/shift-assignments/999", headers=company_headers).status_code == 404


def test_only_active_shifts_and_employees(client, company_headers):
    employee_id, _ = employee_with_face(client, company_headers)
    site = create_site(client, company_headers)
    shift = create_shift(client, company_headers, sites=[site["id"]])
    today = business_today()
    weekend = create_shift(client, company_headers, name="Fin de semana", weekdays=[5, 6])
    client.patch(f"{SHIFTS}/{shift['id']}/status", json={"active": False}, headers=company_headers)
    assert assign(client, company_headers, employee_id, shift["id"], today).json()["code"] == "SHIFT_INACTIVE"
    with SessionLocal() as db:
        db.get(Employee, employee_id).active = False  # type: ignore[union-attr]
        db.commit()
    assert assign(client, company_headers, employee_id, weekend["id"], today).json()["code"] == "EMPLOYEE_INACTIVE"
    assert assign(client, company_headers, 999, weekend["id"], today).status_code == 404


# ---------------------------------------------------------------- solicitudes de cambio


@pytest.fixture
def requester(client, company_headers) -> dict:
    """Empleado con el turno matutino (en la Planta Norte; lunes y martes remoto) que pide el nocturno
    (en la Planta Sur)."""
    employee_id, headers = employee_with_face(client, company_headers)
    site = create_site(client, company_headers)
    south = create_site(client, company_headers, name="Planta Sur")
    morning = create_shift(client, company_headers, sites=[site["id"]], remote=[0, 1])
    night = create_shift(
        client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00", sites=[south["id"]]
    )
    assert assign(client, company_headers, employee_id, morning["id"], business_today()).status_code == 201
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
    # La empresa ve dónde checaría con el turno pedido; el turno actual va resumido.
    assert [s["name"] for s in inbox["items"][0]["shift"]["sites"]] == ["Planta Sur"]
    assert inbox["items"][0]["current_shift"]["remote_weekdays"] == [0, 1]
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
    # Dónde checa lo dice el turno pedido (no se conserva el lugar del anterior).
    assert scheduled["shift"]["name"] == "Nocturno" and scheduled["state"] == "SCHEDULED"
    assert scheduled["shift"]["remote_weekdays"] == [] and [s["name"] for s in scheduled["shift"]["sites"]] == [
        "Planta Sur"
    ]
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
        f"/api/shift-requests/{third['id']}/approve", json={"valid_from": other_day}, headers=company_headers
    )
    assert approved_request.status_code == 200 and approved_request.json()["data"]["valid_from"] == future
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
