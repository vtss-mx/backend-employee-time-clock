"""Asistencia por turno: entrada, descansos y salida con rostro y ubicación, a la hora del servidor.

El reloj del servicio se fija (`clock`) para recorrer una jornada completa, turnos nocturnos, turnos
que se cruzan y jornadas que vencen sin salida. Las asignaciones empiezan hoy y los días de prueba son
posteriores, así que siempre rigen.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.clock import business_today
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import WorkBreak, WorkSession
from app.repositories.attendance_repository import close_missed_checkouts
from app.services import attendance_service, maintenance_service
from app.services.attendance_overview import board_state
from app.services.shift_rules import occurrence_of
from tests.conftest import turn_files
from tests.test_policy import set_policy
from tests.test_shifts import POINT, assign, create_shift, create_site, employee_with_face, shift_body
from tests.test_validators import identify_face, validator_headers

NEAR_KM = (29.0829, -110.9559)  # ~1.1 km al norte del sitio
TIJUANA = (32.5149, -117.0382)  # ~700 km


def local(day: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(settings.APP_TIMEZONE)).astimezone(UTC)


def server_clock(monkeypatch):
    """Fija la hora del servidor para la asistencia: devuelve set_to(día, "HH:MM") en la hora del negocio
    (las pruebas de otros módulos la usan con su propio fixture `clock`)."""
    moment = {"now": datetime.now(UTC)}
    monkeypatch.setattr(attendance_service, "now_utc", lambda: moment["now"])

    def set_to(day: date, hhmm: str) -> datetime:
        moment["now"] = local(day, hhmm)
        return moment["now"]

    return set_to


@pytest.fixture
def clock(monkeypatch):
    return server_clock(monkeypatch)


@pytest.fixture
def worker(client, company_headers) -> dict:
    """Ana con el turno matutino (8:00-16:00, un descanso de 30 min) desde hoy en la Planta Norte."""
    employee_id, headers = employee_with_face(client, company_headers)
    site = create_site(client, company_headers)
    shift = create_shift(client, company_headers, sites=[site["id"]])
    assert assign(client, company_headers, employee_id, shift["id"], business_today()).status_code == 201
    return {
        "id": employee_id,
        "headers": headers,
        "site": site,
        "shift": shift,
        "day": business_today() + timedelta(days=7),
    }


def act(client, headers, action: str, *, at=POINT, accuracy: float = 10, person: str = "ana"):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"c{i}.jpg", f"face:{person}".encode(), "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, person if person == "ana" else "ana")
    data = {"challenge_id": challenge["challenge_id"], "latitude": at[0], "longitude": at[1], "accuracy": accuracy}
    return client.post(f"/api/me/attendance/{action}", data=data, files=files, headers=headers)


def today(client, headers) -> dict:
    response = client.get("/api/me/attendance/today", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def recorded(response) -> dict:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == "ATTENDANCE_RECORDED" and body["data"]["verified"] is True, body
    return body["data"]["session"]


# ---------------------------------------------------------------- una jornada completa


def test_a_full_day_with_a_break_and_an_early_exit(client, company_headers, worker, clock):
    headers, day = worker["headers"], worker["day"]
    clock(day, "07:00")
    before = today(client, headers)
    assert before["actions"] == [] and before["next_occurrence"]["work_date"] == day.isoformat()
    assert before["message"].startswith("Tu siguiente turno es el") and "07:45" in before["message"]
    # Antes de su ventana ya sabe qué turno le toca y dónde checará.
    assert before["shift"]["name"] == "Matutino" and before["sites"][0]["name"] == "Planta Norte"
    assert before["remote_allowed"] is False

    clock(day, "07:50")
    ready = today(client, headers)
    assert ready["actions"] == ["CHECK_IN"] and ready["message"] == "Tu turno es de 08:00 a 16:00: registra tu entrada."
    assert ready["sites"][0]["name"] == "Planta Norte" and ready["remote_allowed"] is False
    session = recorded(act(client, headers, "check-in"))
    assert (
        session["status"] == "OPEN"
        and session["check_in_mode"] == "ON_SITE"
        and session["check_in_site"] == "Planta Norte"
    )
    assert session["late_minutes"] == 0 and session["shift_name"] == "Matutino"
    twice = act(client, headers, "check-in")
    assert twice.status_code == 409 and twice.json()["code"] == "ATTENDANCE_ACTION_NOT_ALLOWED"
    assert today(client, headers)["message"].startswith("En turno desde las 07:50")

    clock(day, "12:00")
    recorded(act(client, headers, "break-start"))
    on_break = today(client, headers)
    assert on_break["actions"] == ["BREAK_END", "CHECK_OUT"] and on_break["message"] == "En descanso desde las 12:00."
    clock(day, "12:40")
    after_break = recorded(act(client, headers, "break-end"))
    assert after_break["break_minutes"] == 40 and after_break["breaks"][0]["exceeded_minutes"] == 10
    assert today(client, headers)["actions"] == ["CHECK_OUT"]  # su único descanso ya se usó
    assert act(client, headers, "break-start").json()["code"] == "ATTENDANCE_ACTION_NOT_ALLOWED"

    clock(day, "15:50")
    closed = recorded(act(client, headers, "check-out"))
    assert closed["status"] == "CLOSED" and closed["early_leave_minutes"] == 10 and closed["worked_minutes"] == 440
    done = today(client, headers)
    assert (
        done["actions"] == []
        and done["message"] == "Ya registraste este turno."
        and done["session"]["id"] == closed["id"]
    )

    # La empresa ve la jornada con la evidencia de cada registro.
    history = client.get("/api/attendance/sessions", params={"status": "CLOSED"}, headers=company_headers).json()[
        "data"
    ]
    assert history["total"] == 1 and history["items"][0]["employee"]["employee_number"] == "EMP-001"
    detail = client.get(f"/api/attendance/sessions/{closed['id']}", headers=company_headers).json()["data"]
    assert [e["action"] for e in detail["events"]] == ["CHECK_IN", "BREAK_START", "BREAK_END", "CHECK_OUT"]
    assert detail["events"][0]["operator"] == "ana@empresa.com" and detail["events"][0]["confidence"] is not None
    assert detail["events"][0]["site"] == "Planta Norte" and detail["events"][0]["distance_m"] < 1
    mine = client.get("/api/me/attendance/history", headers=headers).json()["data"]
    assert mine["items"][0]["worked_minutes"] == 440
    assert (
        client.get("/api/attendance/sessions/999", headers=company_headers).json()["code"] == "WORK_SESSION_NOT_FOUND"
    )


def test_late_arrival_beyond_the_tolerance(client, worker, clock):
    clock(worker["day"], "08:25")
    assert recorded(act(client, worker["headers"], "check-in"))["late_minutes"] == 25
    clock(worker["day"] + timedelta(days=1), "08:09")
    # Al día siguiente: la jornada anterior venció sin salida y esta entra dentro de la tolerancia.
    assert recorded(act(client, worker["headers"], "check-in"))["late_minutes"] == 0


# ---------------------------------------------------------------- ubicación e identidad


def test_location_must_be_precise_and_inside_the_site(client, company_headers, worker, clock):
    headers = worker["headers"]
    clock(worker["day"], "07:55")
    imprecise = act(client, headers, "check-in", accuracy=500)
    assert imprecise.status_code == 422 and imprecise.json()["code"] == "LOCATION_INACCURATE"
    outside = act(client, headers, "check-in", at=NEAR_KM)
    assert outside.status_code == 403 and outside.json()["code"] == "LOCATION_OUT_OF_SITE"
    assert "1.1 km de Planta Norte" in outside.json()["message"]
    set_policy(client, company_headers, max_location_accuracy_m=1000)
    assert recorded(act(client, headers, "check-in", accuracy=500))["check_in_mode"] == "ON_SITE"


def test_editing_the_shift_place_applies_to_everyone_from_then_on(client, company_headers, worker, clock):
    """Los sitios y los días remotos son los del turno de hoy: cambiarlos aplica a la jornada abierta (su
    salida) y a las siguientes; lo registrado conserva dónde se checó."""
    headers, day, shift = worker["headers"], worker["day"], worker["shift"]
    clock(day, "07:55")
    assert recorded(act(client, headers, "check-in"))["check_in_site"] == "Planta Norte"
    # La empresa vuelve remoto el turno (sin sitios): la salida ya se checa desde cualquier lugar.
    assert client.put(f"/api/shifts/{shift['id']}", json=shift_body(), headers=company_headers).status_code == 200
    clock(day, "16:00")
    now = today(client, headers)
    assert now["remote_allowed"] is True and now["sites"] == [] and now["shift"]["remote_weekdays"] == list(range(7))
    closed = recorded(act(client, headers, "check-out", at=NEAR_KM))
    assert closed["check_in_site"] == "Planta Norte" and closed["check_out_mode"] == "REMOTE"
    # El sitio ya no está en ningún turno, pero ahí se checó: su historial lo nombra.
    gone = client.delete(f"/api/sites/{worker['site']['id']}", headers=company_headers)
    assert gone.status_code == 409 and gone.json()["code"] == "SITE_HAS_RECORDS"


def test_remote_days_accept_any_place(client, company_headers, clock):
    employee_id, headers = employee_with_face(client, company_headers)
    shift = create_shift(client, company_headers)  # todos sus días son remotos
    assert assign(client, company_headers, employee_id, shift["id"], business_today()).status_code == 201
    clock(business_today() + timedelta(days=3), "08:00")
    assert today(client, headers)["remote_allowed"] is True and today(client, headers)["sites"] == []
    session = recorded(act(client, headers, "check-in", at=NEAR_KM))
    assert session["check_in_mode"] == "REMOTE" and session["check_in_site"] is None


def test_without_the_face_nothing_is_recorded(client, worker, clock):
    clock(worker["day"], "08:00")
    wrong = act(client, worker["headers"], "check-in", person="beto")
    assert wrong.status_code == 200 and wrong.json()["code"] == "IDENTITY_NOT_VERIFIED"
    assert wrong.json()["data"]["verified"] is False and wrong.json()["data"]["session"] is None
    assert today(client, worker["headers"])["actions"] == ["CHECK_IN"]


def test_impossible_travel_is_rejected(client, company_headers, clock):
    employee_id, headers = employee_with_face(client, company_headers)
    shift = create_shift(client, company_headers)
    assign(client, company_headers, employee_id, shift["id"], business_today())
    day = business_today() + timedelta(days=2)
    clock(day, "07:50")
    recorded(act(client, headers, "check-in"))
    clock(day, "08:10")
    jump = act(client, headers, "break-start", at=TIJUANA)  # ~700 km en 20 minutos
    assert jump.status_code == 403 and jump.json()["code"] == "IMPOSSIBLE_TRAVEL"
    assert jump.json()["errors"][0]["details"]["minutes"] == 20
    set_policy(client, company_headers, detect_impossible_travel=False)
    recorded(act(client, headers, "break-start", at=TIJUANA))


# ---------------------------------------------------------------- turnos nocturnos y que se cruzan


def test_overnight_shift_and_the_next_morning_shift(client, company_headers, clock):
    employee_id, headers = employee_with_face(client, company_headers)
    site = create_site(client, company_headers)
    night = create_shift(
        client,
        company_headers,
        name="Nocturno",
        start_time="22:00",
        end_time="06:00",
        breaks_count=0,
        sites=[site["id"]],
    )
    morning = create_shift(
        client,
        company_headers,
        name="Madrugada",
        start_time="06:00",
        end_time="14:00",
        early_check_in_minutes=30,
        sites=[site["id"]],
    )
    start = business_today()
    assign(client, company_headers, employee_id, night["id"], start)
    change = start + timedelta(days=3)
    assert assign(client, company_headers, employee_id, morning["id"], change).status_code == 201

    clock(change - timedelta(days=1), "21:50")
    recorded(act(client, headers, "check-in"))
    clock(change, "02:00")  # de madrugada sigue en el turno que entró ayer
    midnight = today(client, headers)
    assert (
        midnight["actions"] == ["CHECK_OUT"]
        and midnight["session"]["work_date"] == (change - timedelta(days=1)).isoformat()
    )
    clock(change, "06:05")
    closed = recorded(act(client, headers, "check-out"))
    assert closed["worked_minutes"] == 495 and closed["early_leave_minutes"] == 0

    # A las 6:10 se cruzan el nocturno (su límite de salida) y el matutino: aplica el de entrada más cercana.
    clock(change, "06:10")
    now = today(client, headers)
    assert now["actions"] == ["CHECK_IN"] and now["shift"]["name"] == "Madrugada"
    assert recorded(act(client, headers, "check-in"))["shift_name"] == "Madrugada"


def test_missed_check_out_is_closed_by_maintenance(client, company_headers, worker, clock):
    headers, day = worker["headers"], worker["day"]
    clock(day, "07:50")
    session = recorded(act(client, headers, "check-in"))
    clock(day, "12:00")
    recorded(act(client, headers, "break-start"))
    clock(day + timedelta(days=1), "10:00")
    # Venció su límite (17:00): ya no hay jornada abierta y la de hoy se puede checar (con retardo).
    assert today(client, headers)["actions"] == ["CHECK_IN"]
    board = client.get("/api/attendance/board", params={"date": day.isoformat()}, headers=company_headers).json()[
        "data"
    ]
    assert board["items"][0]["state"] == "MISSED_CHECKOUT"
    with SessionLocal() as db:
        assert close_missed_checkouts(db, local(day + timedelta(days=1), "10:00")) == 1
        db.commit()
        stored = db.get(WorkSession, session["id"])
        assert stored.status == "MISSED_CHECKOUT"  # type: ignore[union-attr]
        pending_break = db.query(WorkBreak).filter_by(session_id=session["id"]).one()
        assert pending_break.ended_at is not None  # el descanso abierto terminó en el límite
    # El mantenimiento programado también lo hace (y lo cuenta).
    assert maintenance_service.purge_expired(SessionLocal())["jornadas sin salida"] == 0


def test_a_new_check_in_closes_the_expired_session_and_its_break(client, worker, clock):
    """Sin esperar al mantenimiento: al registrar, la jornada vencida queda sin salida y su descanso
    abierto termina en el límite de salida."""
    headers, day = worker["headers"], worker["day"]
    clock(day, "07:55")
    first = recorded(act(client, headers, "check-in"))
    clock(day, "12:00")
    recorded(act(client, headers, "break-start"))
    clock(day + timedelta(days=1), "07:55")
    assert recorded(act(client, headers, "check-in"))["work_date"] == (day + timedelta(days=1)).isoformat()
    with SessionLocal() as db:
        expired = db.get(WorkSession, first["id"])
        assert expired is not None and expired.status == "MISSED_CHECKOUT"
        pending_break = db.query(WorkBreak).filter_by(session_id=first["id"]).one()
        assert pending_break.ended_at == expired.check_out_deadline


def test_checking_out_during_a_break_ends_it(client, worker, clock):
    headers, day = worker["headers"], worker["day"]
    clock(day, "08:00")
    recorded(act(client, headers, "check-in"))
    clock(day, "15:30")
    recorded(act(client, headers, "break-start"))
    clock(day, "16:00")
    closed = recorded(act(client, headers, "check-out"))
    assert closed["status"] == "CLOSED" and closed["break_minutes"] == 30 and closed["worked_minutes"] == 450
    assert closed["breaks"][0]["ended_at"] is not None and closed["breaks"][0]["exceeded_minutes"] == 0


def test_an_inactive_shift_cannot_be_checked(client, company_headers, worker, clock):
    clock(worker["day"], "08:00")
    client.patch(f"/api/shifts/{worker['shift']['id']}/status", json={"active": False}, headers=company_headers)
    paused = today(client, worker["headers"])
    assert paused["actions"] == [] and paused["shift"] is None and paused["next_occurrence"] is None
    refused = act(client, worker["headers"], "check-in")
    assert refused.status_code == 409 and refused.json()["code"] == "ATTENDANCE_ACTION_NOT_ALLOWED"


def test_maintenance_survives_a_failing_close(monkeypatch):
    from sqlalchemy.exc import OperationalError

    def broken(db, now):
        raise OperationalError("UPDATE", {}, Exception("bloqueo"))

    monkeypatch.setattr(maintenance_service, "close_missed_checkouts", broken)
    assert maintenance_service.purge_expired(SessionLocal())["jornadas sin salida"] == 0


# ---------------------------------------------------------------- validadores


def test_validator_identifications_count_as_check_in_and_out(client, company_headers, worker, clock):
    headers = validator_headers(client, company_headers, mode="FACE")
    day = worker["day"]
    clock(day, "07:55")
    first = identify_face(client, headers, "ana").json()["data"]
    assert first["verified"] is True and first["attendance"] == {
        "action": "CHECK_IN",
        "message": "Entrada registrada a las 07:55.",
    }
    clock(day, "08:10")
    double = identify_face(client, headers, "ana").json()["data"]["attendance"]
    assert double == {"action": None, "message": "Entrada ya registrada a las 07:55."}
    clock(day, "16:05")
    assert identify_face(client, headers, "ana").json()["data"]["attendance"]["action"] == "CHECK_OUT"
    clock(day, "16:10")
    assert identify_face(client, headers, "ana").json()["data"]["attendance"] == {
        "action": None,
        "message": "Sin turno para registrar en este momento.",
    }
    sessions = client.get("/api/attendance/sessions", headers=company_headers).json()["data"]
    assert (
        sessions["items"][0]["check_in_mode"] == "VALIDATOR" and sessions["items"][0]["check_out_mode"] == "VALIDATOR"
    )
    detail = client.get(f"/api/attendance/sessions/{sessions['items'][0]['id']}", headers=company_headers).json()[
        "data"
    ]
    assert detail["events"][0]["operator"] == "Recepción" and detail["events"][0]["latitude"] is None


# ---------------------------------------------------------------- tablero e historial


def test_board_of_the_day(client, company_headers, worker, clock):
    other_id, _ = employee_with_face(client, company_headers, person="beto", number="EMP-002")
    assign(client, company_headers, other_id, worker["shift"]["id"], business_today())
    day = worker["day"]
    clock(day, "07:00")
    early = client.get("/api/attendance/board", params={"date": day.isoformat()}, headers=company_headers).json()[
        "data"
    ]
    assert {r["employee"]["employee_number"]: r["state"] for r in early["items"]} == {
        "EMP-001": "SCHEDULED",
        "EMP-002": "SCHEDULED",
    }
    clock(day, "07:55")
    recorded(act(client, worker["headers"], "check-in"))
    clock(day, "09:00")
    board = client.get("/api/attendance/board", params={"date": day.isoformat()}, headers=company_headers).json()[
        "data"
    ]
    states = {r["employee"]["employee_number"]: r["state"] for r in board["items"]}
    assert states == {"EMP-001": "WORKING", "EMP-002": "MISSING"}
    assert (board["working"], board["on_break"], board["done"], board["missed_checkout"]) == (1, 0, 0, 0)
    assert board["items"][0]["department"] is None and board["work_date"] == day.isoformat()
    clock(day, "12:00")
    recorded(act(client, worker["headers"], "break-start"))
    found = client.get(
        "/api/attendance/board", params={"date": day.isoformat(), "search": "emp-001"}, headers=company_headers
    ).json()["data"]
    assert found["total"] == 1 and found["items"][0]["state"] == "ON_BREAK" and found["on_break"] == 1
    clock(day, "18:00")
    late = client.get("/api/attendance/board", params={"date": day.isoformat()}, headers=company_headers).json()["data"]
    assert {r["employee"]["employee_number"]: r["state"] for r in late["items"]}["EMP-002"] == "ABSENT"
    today_board = client.get("/api/attendance/board", headers=company_headers).json()["data"]
    assert today_board["work_date"] == business_today().isoformat()


def test_history_filters(client, company_headers, worker, clock):
    day = worker["day"]
    clock(day, "08:00")
    recorded(act(client, worker["headers"], "check-in"))
    params = {"start": day.isoformat(), "end": day.isoformat(), "employee_id": worker["id"]}
    assert client.get("/api/attendance/sessions", params=params, headers=company_headers).json()["data"]["total"] == 1
    today = {"start": business_today().isoformat(), "end": business_today().isoformat()}
    assert client.get("/api/attendance/sessions", params=today, headers=company_headers).json()["data"]["total"] == 0
    inverted = {"start": day.isoformat(), "end": (day - timedelta(days=1)).isoformat()}
    assert (
        client.get("/api/attendance/sessions", params=inverted, headers=company_headers).json()["code"]
        == "ATTENDANCE_INVALID_PERIOD"
    )


def test_board_state_rules():
    """En qué va cada empleado según su jornada (y la hora)."""
    zone = ZoneInfo(settings.APP_TIMEZONE)
    day = date(2026, 3, 2)

    class Morning:
        start_time = datetime(2026, 1, 1, 8).time()
        end_time = datetime(2026, 1, 1, 16).time()
        early_check_in_minutes = 15
        late_check_out_minutes = 60

    occurrence = occurrence_of(Morning(), day, zone)  # type: ignore[arg-type]
    open_session = WorkSession(status="OPEN", check_out_deadline=occurrence.deadline)
    assert board_state(open_session, False, occurrence, occurrence.start) == "WORKING"
    assert board_state(open_session, True, occurrence, occurrence.start) == "ON_BREAK"
    assert board_state(WorkSession(status="CLOSED"), False, occurrence, occurrence.end) == "DONE"
    assert board_state(None, False, occurrence, occurrence.deadline + timedelta(minutes=1)) == "ABSENT"
