"""La asistencia respeta el calendario y los horarios: en un día libre no se checa, se entra solo
dentro de la ventana del turno y el descanso es libre pero dentro del horario."""

from datetime import datetime, timedelta

import pytest

from app.core.clock import business_today
from tests.test_attendance import act, local, recorded, server_clock, today
from tests.test_shifts import assign, create_shift, employee_with_face
from tests.test_validators import identify_face, validator_headers

CAL = "/api/calendar"


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture
def clock(monkeypatch):
    return server_clock(monkeypatch)


def remote_worker(client, company_headers) -> dict:
    """Ana con el turno matutino (8:00-16:00, un descanso de 30 min) desde hoy, remoto todos los días."""
    employee_id, headers = employee_with_face(client, company_headers)
    shift = create_shift(client, company_headers)
    assert assign(client, company_headers, employee_id, shift["id"], business_today()).status_code == 201
    return {"id": employee_id, "headers": headers, "shift": shift, "day": business_today() + timedelta(days=7)}


@pytest.fixture
def worker(client, company_headers) -> dict:
    return remote_worker(client, company_headers)


def holiday(client, headers, day, name="Navidad") -> None:
    response = client.post(f"{CAL}/holidays", json={"holiday_date": day.isoformat(), "name": name}, headers=headers)
    assert response.status_code == 201, response.text


def vacation(client, headers, employee_ids, start, days=3, kind="VACATION") -> None:
    body = {
        "employee_ids": employee_ids,
        "type": kind,
        "starts_on": start.isoformat(),
        "ends_on": (start + timedelta(days=days - 1)).isoformat(),
    }
    assert client.post(f"{CAL}/absences", json=body, headers=headers).json()["data"]["done"] == len(employee_ids)


def board(client, headers, day) -> dict:
    return client.get("/api/attendance/board", params={"date": day.isoformat()}, headers=headers).json()["data"]


# ---------------------------------------------------------------- días libres


def test_no_check_in_on_a_holiday_unless_it_is_a_workday(client, company_headers, worker, clock):
    headers, day = worker["headers"], worker["day"]
    holiday(client, company_headers, day)
    clock(day, "07:50")
    off = today(client, headers)
    assert off["actions"] == [] and off["occurrence"] is None
    assert off["day_off"] == {
        "kind": "HOLIDAY",
        "name": "Navidad",
        "work_date": day.isoformat(),
        "starts_on": day.isoformat(),
        "ends_on": day.isoformat(),
    }
    assert off["message"].startswith("Hoy es día festivo: Navidad. Tu siguiente turno es el ")
    assert off["next_occurrence"]["work_date"] == (day + timedelta(days=1)).isoformat()
    refused = act(client, headers, "check-in")
    assert refused.status_code == 409 and refused.json()["code"] == "DAY_OFF"
    assert refused.json()["message"] == "Hoy es día festivo: Navidad"
    assert act(client, headers, "break-start").json()["code"] == "DAY_OFF"

    # La empresa la necesita ese día: lo marca como laborable solo para ella.
    workday = {"employee_id": worker["id"], "work_date": day.isoformat(), "note": "Inventario"}
    assert client.post(f"{CAL}/workdays", json=workday, headers=company_headers).status_code == 201
    assert today(client, headers)["actions"] == ["CHECK_IN"]
    assert recorded(act(client, headers, "check-in"))["work_date"] == day.isoformat()


def test_vacation_blocks_the_employee_and_the_validator(client, company_headers, worker, clock):
    headers, day = worker["headers"], worker["day"]
    vacation(client, company_headers, [worker["id"]], day)
    clock(day + timedelta(days=1), "09:00")
    off = today(client, headers)
    expected = f"Estás de vacaciones del {day:%d/%m/%Y} al {day + timedelta(days=2):%d/%m/%Y}"
    assert off["message"].startswith(expected) and off["day_off"]["kind"] == "VACATION"
    assert off["day_off"]["name"] == "Vacaciones" and off["day_off"]["ends_on"] == (day + timedelta(days=2)).isoformat()
    assert act(client, headers, "check-in").json()["message"] == expected
    validator = validator_headers(client, company_headers, mode="FACE")
    assert identify_face(client, validator, "ana").json()["data"]["attendance"] == {
        "action": None,
        "message": f"Sin registro: {expected}.",
    }


def test_between_shifts_the_day_off_of_today_is_shown(client, company_headers, worker, clock):
    day = worker["day"]
    holiday(client, company_headers, day, "Día de la empresa")
    clock(day, "20:00")  # fuera de la ventana del turno de hoy
    off = today(client, worker["headers"])
    assert off["day_off"]["name"] == "Día de la empresa" and off["actions"] == []
    assert off["message"].startswith("Hoy es día festivo: Día de la empresa. Tu siguiente turno es el")


def test_the_next_shift_skips_days_off(client, company_headers, worker, clock):
    day = worker["day"]
    holiday(client, company_headers, day)
    clock(day - timedelta(days=1), "20:00")
    upcoming = today(client, worker["headers"])
    assert upcoming["next_occurrence"]["work_date"] == (day + timedelta(days=1)).isoformat()
    assert upcoming["day_off"] is None and upcoming["message"].startswith("Tu siguiente turno es el")

    # Todo el horizonte son vacaciones: dice por qué no hay siguiente turno.
    vacation(client, company_headers, [worker["id"]], day + timedelta(days=1), days=10)
    clock(day - timedelta(days=1), "20:00")
    away = today(client, worker["headers"])
    assert away["next_occurrence"] is None and away["shift"] is None
    assert away["day_off"]["kind"] == "HOLIDAY" and away["message"] == f"El {day:%d/%m/%Y} es día festivo: Navidad."


def test_an_overnight_shift_belongs_to_the_day_it_starts(client, company_headers, clock):
    employee_id, headers = employee_with_face(client, company_headers)
    night = create_shift(client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00", breaks_count=0)
    assign(client, company_headers, employee_id, night["id"], business_today())
    day = business_today() + timedelta(days=3)
    holiday(client, company_headers, day + timedelta(days=1), "Al día siguiente")
    clock(day, "21:50")  # entra la víspera del festivo: su jornada es de hoy
    recorded(act(client, headers, "check-in"))
    clock(day + timedelta(days=1), "06:00")
    recorded(act(client, headers, "check-out"))
    clock(day + timedelta(days=1), "21:50")  # la jornada que empieza el festivo no se checa
    assert act(client, headers, "check-in").json()["code"] == "DAY_OFF"
    clock(day + timedelta(days=2), "02:00")  # de madrugada sigue siendo la jornada del festivo
    assert today(client, headers)["day_off"]["work_date"] == (day + timedelta(days=1)).isoformat()


def test_the_board_shows_days_off_instead_of_absences(client, company_headers, worker, clock):
    beto, _ = employee_with_face(client, company_headers, person="beto", number="EMP-002")
    assign(client, company_headers, beto, worker["shift"]["id"], business_today())
    day = worker["day"]
    vacation(client, company_headers, [worker["id"]], day, days=1)
    clock(day, "18:00")
    states = board(client, company_headers, day)
    rows = {r["employee"]["employee_number"]: r for r in states["items"]}
    assert rows["EMP-001"]["state"] == "DAY_OFF" and rows["EMP-001"]["day_off"]["name"] == "Vacaciones"
    assert rows["EMP-002"]["state"] == "ABSENT" and rows["EMP-002"]["day_off"] is None
    assert states["day_off"] == 1

    # Un festivo: los dos tienen el día libre, salvo quien lo tiene como laborable (y checó).
    other_day = day + timedelta(days=1)
    holiday(client, company_headers, other_day)
    clock(other_day, "07:00")
    assert board(client, company_headers, other_day)["day_off"] == 2
    workday = {"employee_id": beto, "work_date": other_day.isoformat()}
    assert client.post(f"{CAL}/workdays", json=workday, headers=company_headers).status_code == 201
    clock(other_day, "08:00")
    checked = identify_face(client, validator_headers(client, company_headers, mode="FACE"), "beto").json()["data"]
    assert checked["attendance"]["action"] == "CHECK_IN"
    holiday_board = board(client, company_headers, other_day)
    rows = {r["employee"]["employee_number"]: r["state"] for r in holiday_board["items"]}
    assert rows == {"EMP-001": "DAY_OFF", "EMP-002": "WORKING"} and holiday_board["day_off"] == 1


# ---------------------------------------------------------------- ventanas del turno y descansos


def test_the_break_is_free_but_within_working_hours(client, worker, clock):
    headers, day = worker["headers"], worker["day"]
    clock(day, "07:50")
    recorded(act(client, headers, "check-in"))
    early = today(client, headers)
    assert early["actions"] == ["CHECK_OUT"]  # aún no empieza su horario
    window = early["break_window"]
    assert window["minutes"] == 30 and window["remaining"] == 1
    assert parse(window["starts_at"]) == local(day, "08:00") and parse(window["ends_at"]) == local(day, "16:00")
    too_soon = act(client, headers, "break-start")
    assert too_soon.status_code == 409 and too_soon.json()["code"] == "ATTENDANCE_ACTION_NOT_ALLOWED"
    assert too_soon.json()["message"] == "Puedes tomar tu descanso entre las 08:00 y las 16:00"
    clock(day, "08:00")
    assert today(client, headers)["actions"] == ["BREAK_START", "CHECK_OUT"]
    clock(day, "15:59")
    recorded(act(client, headers, "break-start"))
    on_break = act(client, headers, "break-start")
    assert on_break.json()["message"] == "Ya estás en descanso"
    clock(day, "16:20")  # terminar el descanso siempre se puede
    after = recorded(act(client, headers, "break-end"))
    assert after["break_minutes"] == 21 and after["breaks"][0]["exceeded_minutes"] == 0
    used = act(client, headers, "break-start")
    assert used.json()["message"] == "Ya tomaste los descansos de este turno"
    assert today(client, headers)["break_window"]["remaining"] == 0


def test_no_break_after_the_scheduled_end(client, company_headers, clock):
    employee_id, headers = employee_with_face(client, company_headers)
    shift = create_shift(client, company_headers, breaks_count=2, break_minutes=15)
    assign(client, company_headers, employee_id, shift["id"], business_today())
    day = business_today() + timedelta(days=2)
    clock(day, "08:00")
    recorded(act(client, headers, "check-in"))
    clock(day, "16:05")  # ya pasó su salida (aún puede checarla)
    assert today(client, headers)["actions"] == ["CHECK_OUT"]
    late = act(client, headers, "break-start")
    assert late.status_code == 409 and "entre las 08:00 y las 16:00" in late.json()["message"]


def test_check_in_only_within_the_shift(client, company_headers, worker, clock):
    headers, day = worker["headers"], worker["day"]
    clock(day, "16:10")  # su turno ya terminó y no entró
    missed = today(client, headers)
    assert missed["actions"] == [] and missed["occurrence"]["work_date"] == day.isoformat()
    assert missed["message"].startswith("Tu turno terminó a las 16:00 sin registrar tu entrada. Tu siguiente turno")
    refused = act(client, headers, "check-in")
    assert refused.status_code == 409
    assert refused.json()["message"] == "Tu turno terminó a las 16:00: ya no puedes registrar tu entrada"
    for action, message in (
        ("break-start", "No tienes una entrada registrada"),
        ("break-end", "No tienes un descanso en curso"),
        ("check-out", "No tienes una entrada registrada para checar tu salida"),
    ):
        assert act(client, headers, action).json()["message"] == message, action
    clock(day, "03:00")  # sin jornada en este momento
    assert act(client, headers, "check-in").json()["message"] == "No tienes un turno en este momento"
    clock(day, "08:00")
    recorded(act(client, headers, "check-in"))
    assert act(client, headers, "check-in").json()["message"] == "Ya tienes una entrada registrada"
    clock(day, "16:00")
    recorded(act(client, headers, "check-out"))
    assert act(client, headers, "check-in").json()["message"] == "Ya registraste este turno"
