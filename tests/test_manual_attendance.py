"""La empresa registra la jornada de quien no checó o la corrige (solo la empresa, sin rostro ni
ubicación, con motivo): mismas reglas y mismos cálculos que el registro en vivo."""

from datetime import timedelta

import pytest

from app.core.clock import business_today
from app.core.database import SessionLocal
from app.models import Employee, WorkSession
from tests.conftest import create_company, login
from tests.test_attendance import act, local, recorded, server_clock
from tests.test_attendance_calendar import holiday, remote_worker
from tests.test_shifts import assign, create_shift, employee_with_face

SESSIONS = "/api/attendance/sessions"


@pytest.fixture
def clock(monkeypatch):
    return server_clock(monkeypatch)


@pytest.fixture
def worker(client, company_headers) -> dict:
    return remote_worker(client, company_headers)


def manual(employee_id: int, day, **changes) -> dict:
    return {
        "employee_id": employee_id,
        "work_date": day.isoformat(),
        "check_in": "08:05",
        "check_out": "16:00",
        "breaks": [{"start": "12:00", "end": "12:45"}],
        "reason": "Olvidó checar",
        **changes,
    }


def test_the_company_records_a_day_the_employee_did_not_check(client, company_headers, worker, clock):
    day = worker["day"]
    clock(day, "18:00")
    created = client.post(SESSIONS, json=manual(worker["id"], day), headers=company_headers)
    assert created.status_code == 201 and created.json()["code"] == "ATTENDANCE_SESSION_CREATED"
    session = created.json()["data"]
    assert (
        session["status"] == "CLOSED"
        and session["check_in_mode"] == "COMPANY"
        and session["check_out_mode"] == "COMPANY"
    )
    assert session["late_minutes"] == 0 and session["early_leave_minutes"] == 0  # dentro de sus tolerancias
    assert session["break_minutes"] == 45 and session["breaks"][0]["exceeded_minutes"] == 15
    assert session["worked_minutes"] == 430 and session["edit_reason"] == "Olvidó checar" and session["edited_at"]
    events = session["events"]
    assert [e["action"] for e in events] == ["CHECK_IN", "BREAK_START", "BREAK_END", "CHECK_OUT"]
    assert {e["mode"] for e in events} == {"COMPANY"} and events[0]["note"] == "Olvidó checar"
    assert events[0]["operator"] == "admin@empresa.com" and events[0]["latitude"] is None

    again = client.post(SESSIONS, json=manual(worker["id"], day), headers=company_headers)
    assert again.status_code == 409 and again.json()["code"] == "ATTENDANCE_SESSION_EXISTS"
    # El empleado lo ve en su historial (de solo lectura) con el motivo.
    mine = client.get("/api/me/attendance/history", headers=worker["headers"]).json()["data"]["items"][0]
    assert mine["edit_reason"] == "Olvidó checar" and mine["check_in_mode"] == "COMPANY"
    board = client.get("/api/attendance/board", params={"date": day.isoformat()}, headers=company_headers).json()
    assert board["data"]["items"][0]["state"] == "DONE"


def test_what_cannot_be_recorded(client, company_headers, worker, clock, admin_headers):
    day = worker["day"]
    clock(day, "18:00")
    for body, status, code in (
        (manual(999, day), 404, "EMPLOYEE_NOT_FOUND"),
        (manual(worker["id"], business_today() - timedelta(days=1)), 422, "NO_SHIFT_THAT_DAY"),  # aún no tenía turno
        (manual(worker["id"], day, check_in="06:00"), 422, "CHECK_IN_OUTSIDE_SHIFT"),
        (manual(worker["id"], day, check_out="07:00"), 422, "CHECK_OUT_BEFORE_CHECK_IN"),
        (manual(worker["id"], day, check_out="17:30"), 422, "CHECK_OUT_AFTER_DEADLINE"),
        (manual(worker["id"], day, breaks=[{"start": "07:00", "end": "07:10"}]), 422, "BREAK_OUTSIDE_SESSION"),
        (manual(worker["id"], day, breaks=[{"start": "12:00", "end": "12:10"}] * 2), 422, "BREAKS_EXCEEDED"),
        (manual(worker["id"], day, reason="  ok  "), 422, "VALIDATION_ERROR"),
        (manual(worker["id"], day + timedelta(days=1)), 422, "TIME_IN_FUTURE"),
    ):
        response = client.post(SESSIONS, json=body, headers=company_headers)
        assert (response.status_code, response.json()["code"]) == (status, code), body
    assert (
        client.post(SESSIONS, json=manual(worker["id"], day, check_in="06:00"), headers=company_headers).json()[
            "errors"
        ][0]["field"]
        == "check_in"
    )
    # Un día libre se marca primero como laborable.
    holiday(client, company_headers, day)
    off = client.post(SESSIONS, json=manual(worker["id"], day), headers=company_headers)
    assert off.status_code == 409 and off.json()["code"] == "DAY_OFF" and "Calendario" in off.json()["message"]
    # Un turno desactivado o que no trabaja ese día tampoco.
    client.patch(f"/api/shifts/{worker['shift']['id']}/status", json={"active": False}, headers=company_headers)
    assert (
        client.post(SESSIONS, json=manual(worker["id"], day), headers=company_headers).json()["code"]
        == "NO_SHIFT_THAT_DAY"
    )
    # Otra empresa no registra a sus empleados.
    assert create_company(client, admin_headers).status_code == 201
    other = login(client, "admin@panificadora.com", "Empresa1234")
    assert client.post(SESSIONS, json=manual(worker["id"], day), headers=other).status_code == 404


def test_without_check_out_the_session_stays_open_or_missed(client, company_headers, worker, clock):
    day = worker["day"]
    clock(day, "10:00")
    still = client.post(SESSIONS, json=manual(worker["id"], day, check_out=None, breaks=[]), headers=company_headers)
    assert still.status_code == 201 and still.json()["data"]["status"] == "OPEN"
    assert still.json()["data"]["worked_minutes"] is None
    # El empleado sigue su jornada en vivo: su salida, con su rostro.
    clock(day, "16:00")
    closed = recorded(act(client, worker["headers"], "check-out"))
    assert closed["check_out_mode"] == "REMOTE" and closed["check_in_mode"] == "COMPANY"

    next_day = day + timedelta(days=1)
    clock(next_day + timedelta(days=1), "09:00")  # ya venció su límite de salida
    missed = client.post(SESSIONS, json=manual(worker["id"], next_day, check_out=None), headers=company_headers)
    assert missed.json()["data"]["status"] == "MISSED_CHECKOUT"


def _open_session_elsewhere(employee_id: int, deadline) -> int:
    """Una jornada abierta de otro día (para probar que no queden dos abiertas)."""
    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        assert employee is not None
        session = db.query(WorkSession).filter_by(employee_id=employee_id).one()
        session.status = "OPEN"
        session.check_out_at = None
        session.check_out_deadline = deadline
        db.commit()
        return session.id


def test_only_one_open_session(client, company_headers, worker, clock):
    day = worker["day"]
    clock(day - timedelta(days=1), "18:00")
    client.post(SESSIONS, json=manual(worker["id"], day - timedelta(days=1)), headers=company_headers)
    clock(day, "10:00")
    other = _open_session_elsewhere(worker["id"], local(day, "11:00"))
    blocked = client.post(SESSIONS, json=manual(worker["id"], day, check_out=None, breaks=[]), headers=company_headers)
    assert blocked.status_code == 409 and blocked.json()["code"] == "ATTENDANCE_SESSION_OPEN"
    # Si la otra ya venció, se marca sin salida (como al checar) y esta queda abierta.
    _open_session_elsewhere(worker["id"], local(day, "09:00"))
    opened = client.post(SESSIONS, json=manual(worker["id"], day, check_out=None, breaks=[]), headers=company_headers)
    assert opened.status_code == 201 and opened.json()["data"]["status"] == "OPEN"
    with SessionLocal() as db:
        assert db.get(WorkSession, other).status == "MISSED_CHECKOUT"  # type: ignore[union-attr]


def test_the_company_corrects_a_session(client, company_headers, worker, clock, admin_headers):
    headers, day = worker["headers"], worker["day"]
    clock(day, "07:55")
    live = recorded(act(client, headers, "check-in"))
    clock(day, "12:00")
    recorded(act(client, headers, "break-start"))
    clock(day, "12:40")
    recorded(act(client, headers, "break-end"))
    clock(day, "16:00")
    recorded(act(client, headers, "check-out"))
    clock(day, "18:00")
    url = f"{SESSIONS}/{live['id']}"
    body = {
        "check_in": "07:55",
        "check_out": "16:30",
        "breaks": [{"start": "12:00", "end": "12:30"}],
        "reason": "Se quedó a cerrar",
    }
    corrected = client.put(url, json=body, headers=company_headers)
    assert corrected.status_code == 200 and corrected.json()["code"] == "ATTENDANCE_SESSION_CORRECTED"
    session = corrected.json()["data"]
    # La entrada no cambió: conserva cómo se registró; la salida sí.
    assert session["check_in_mode"] == "REMOTE" and session["check_out_mode"] == "COMPANY"
    assert session["break_minutes"] == 30 and session["worked_minutes"] == 485 and len(session["breaks"]) == 1
    assert session["edit_reason"] == "Se quedó a cerrar"
    modes = [e["mode"] for e in session["events"]]
    assert modes == ["REMOTE"] * 4 + ["COMPANY"] * 4  # lo anterior se conserva como evidencia

    later = client.put(url, json={**body, "check_in": "08:30"}, headers=company_headers).json()["data"]
    assert later["check_in_mode"] == "COMPANY" and later["late_minutes"] == 30
    assert later["check_out_mode"] == "COMPANY"  # la salida declarada es la misma: conserva su modalidad
    removed = client.put(url, json={**body, "check_out": None, "breaks": []}, headers=company_headers).json()["data"]
    assert removed["status"] == "MISSED_CHECKOUT" and removed["check_out_at"] is None and removed["breaks"] == []
    wrong = client.put(url, json={**body, "check_out": "07:00"}, headers=company_headers)
    assert wrong.status_code == 422 and wrong.json()["code"] == "CHECK_OUT_BEFORE_CHECK_IN"
    assert client.put(f"{SESSIONS}/999", json=body, headers=company_headers).json()["code"] == "WORK_SESSION_NOT_FOUND"
    assert create_company(client, admin_headers).status_code == 201
    other = login(client, "admin@panificadora.com", "Empresa1234")
    assert client.put(url, json=body, headers=other).status_code == 404


def test_correcting_an_open_session_keeps_it_open(client, company_headers, clock):
    employee_id, headers = employee_with_face(client, company_headers)
    night = create_shift(client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00")
    assign(client, company_headers, employee_id, night["id"], business_today(), remote=range(7))
    day = business_today() + timedelta(days=2)
    clock(day, "21:55")
    live = recorded(act(client, headers, "check-in"))
    clock(day + timedelta(days=1), "02:00")
    body = {"check_in": "22:00", "breaks": [{"start": "01:00", "end": "01:20"}], "reason": "Hora equivocada"}
    fixed = client.put(f"{SESSIONS}/{live['id']}", json=body, headers=company_headers).json()["data"]
    assert fixed["status"] == "OPEN" and fixed["check_in_mode"] == "COMPANY" and fixed["break_minutes"] == 20
    # Sigue su jornada: el descanso de la madrugada ya cuenta y checa su salida del día siguiente.
    clock(day + timedelta(days=1), "06:00")
    closed = recorded(act(client, headers, "check-out"))
    assert closed["worked_minutes"] == 460 and closed["edit_reason"] == "Hora equivocada"
