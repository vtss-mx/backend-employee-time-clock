"""Antifraude 1b en los flujos reales: red de la IP con la base local (nube, otro país que el sitio, cambio repentino de
red), varias lecturas de ubicación (congelada, precisión constante, salto), el 1:N en cada 1:1, la telemetría del
navegador y las tablas JPEG. Todas nacen en "solo medir": se registran sin cambiar la decisión."""

import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import SessionLocal
from app.main import app
from app.models import AttendanceEvent
from app.services.face_gallery import face_galleries
from tests.conftest import IPHONE_UA, create_employee, flash_files, login, submit_enrollment, turn_files
from tests.mmdb_support import MX_HOME, MX_MOBILE, US_CLOUD, US_HOME, install
from tests.test_attendance import clock, recorded, worker  # noqa: F401 (fixtures)
from tests.test_attendance_calendar import remote_worker
from tests.test_policy import admin_policy, set_policy
from tests.test_policy_governance import _assessment, company_id_of
from tests.test_risk_engine import assessments, last_reasons
from tests.test_shifts import POINT
from tests.test_validators import approved, identify_face, validator_headers

TIJUANA = (32.5149, -117.0382)  # ~700 km del sitio
GOOD_TELEMETRY = {
    "v": 1,
    "webdriver": False,
    "automation": 0,
    "virtual_camera": False,
    "track": {"width": 1280, "height": 720, "frame_rate": 30, "device_id": True},
    "frames": {"count": 60, "mean_ms": 33.4, "cv": 0.08},
    "screen": {"width": 390, "height": 844, "pixel_ratio": 3, "touch_points": 5},
}


@contextmanager
def from_ip(ip: str):
    """Otro cliente de la misma app (ya arrancada por el fixture `client`), con la IP pública `ip`: el `client` de las
    pruebas no tiene una IP."""
    other = TestClient(app, headers={"User-Agent": IPHONE_UA}, client=(ip, 50_000))
    try:
        yield other
    finally:
        other.close()


def punch(client, headers, action: str, *, at=POINT, accuracy: float = 12, samples=None, telemetry=GOOD_TELEMETRY):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"c{i}.jpg", b"face:ana", "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "ana")
    data = {"challenge_id": challenge["challenge_id"], "latitude": at[0], "longitude": at[1], "accuracy": accuracy}
    if samples is not None:
        data["location_samples"] = samples if isinstance(samples, str) else json.dumps(samples)
    if telemetry is not None:
        data["telemetry"] = json.dumps(telemetry)
    return client.post(f"/api/me/attendance/{action}", data=data, files=files, headers=headers)


def events() -> list[AttendanceEvent]:
    with SessionLocal() as db:
        return list(db.scalars(select(AttendanceEvent).order_by(AttendanceEvent.id)))


# ---------------------------------------------------------------- red de la IP (base local DB-IP)


def test_a_cloud_ip_from_another_country_than_the_site(client, company_headers, worker, clock):  # noqa: F811
    install()
    clock(worker["day"], "07:50")
    with from_ip(US_CLOUD) as cloud:
        session = recorded(punch(cloud, worker["headers"], "check-in"))
    assert session["review_status"] is None  # solo se mide
    reasons = last_reasons()
    assert {"NETWORK_HOSTING", "NETWORK_COUNTRY_MISMATCH"} <= set(reasons)
    assert reasons["NETWORK_HOSTING"]["mode"] == "OBSERVE" and reasons["NETWORK_HOSTING"]["points"] == 20
    [event] = events()
    assert (event.ip_country, event.ip_asn) == ("US", 16509)  # la red, nunca la IP


def test_a_sudden_change_of_country_or_network_between_punches(client, company_headers, clock):  # noqa: F811
    remote = remote_worker(client, company_headers)
    headers, day = remote["headers"], remote["day"]
    install()
    clock(day, "07:50")
    with from_ip(MX_HOME) as home:
        recorded(punch(home, headers, "check-in"))
    assert "NETWORK_JUMP" not in last_reasons()  # sin registro anterior
    clock(day, "12:00")
    with from_ip(US_HOME) as abroad:
        recorded(punch(abroad, headers, "break-start"))
    assert "NETWORK_JUMP" not in last_reasons()  # otro país, pero 4 h después
    clock(day, "12:05")
    with from_ip(MX_MOBILE) as mobile:
        recorded(punch(mobile, headers, "break-end"))
    assert last_reasons()["NETWORK_JUMP"] == {**last_reasons()["NETWORK_JUMP"], "value": 5.0, "threshold": 120}
    clock(day, "12:08")
    with from_ip(MX_HOME) as home:
        recorded(punch(home, headers, "check-out"))
    assert last_reasons()["NETWORK_JUMP"] == {**last_reasons()["NETWORK_JUMP"], "value": 3.0, "threshold": 10}
    assert [(e.ip_country, e.ip_asn) for e in events()] == [("MX", 22884), ("US", 7922), ("MX", 28403), ("MX", 22884)]


def test_without_the_ip_database_or_a_public_ip_nothing_is_measured(client, company_headers, worker, clock):  # noqa: F811
    clock(worker["day"], "07:50")
    with from_ip(US_CLOUD) as cloud:
        recorded(punch(cloud, worker["headers"], "check-in"))  # sin la base local
    assert not {code for code in last_reasons() if code.startswith("NETWORK_")}
    install()
    clock(worker["day"], "12:00")
    recorded(punch(client, worker["headers"], "break-start"))  # la IP de pruebas no es pública
    assert not {code for code in last_reasons() if code.startswith("NETWORK_")}
    assert [(e.ip_country, e.ip_asn) for e in events()] == [(None, None), (None, None)]


def test_a_verification_without_a_site_only_measures_the_network(client, company_headers):
    approved(client, company_headers, "juan", number="EMP-001")
    headers = login(client, "juan@empresa.com", "Empleado123")
    install()
    with from_ip(US_CLOUD) as cloud:
        challenge = cloud.post("/api/face/challenge", headers=headers).json()["data"]
        files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(2)]
        files += turn_files(challenge, "juan") + flash_files(challenge, "juan")
        verified = cloud.post(
            "/api/verification/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
        )
    assert verified.json()["data"]["verified"] is True
    assert "NETWORK_HOSTING" in last_reasons() and "NETWORK_COUNTRY_MISMATCH" not in last_reasons()


# ---------------------------------------------------------------- varias lecturas de la ubicación


def test_identical_readings_and_a_constant_round_accuracy_look_simulated(client, company_headers, worker, clock):  # noqa: F811
    clock(worker["day"], "07:50")
    frozen = [{"latitude": POINT[0], "longitude": POINT[1], "accuracy": 10.0}] * 3
    recorded(punch(client, worker["headers"], "check-in", accuracy=10, samples=frozen))
    reasons = last_reasons()
    assert reasons["LOCATION_STATIC"] == {**reasons["LOCATION_STATIC"], "value": 3.0, "threshold": 3, "mode": "OBSERVE"}
    assert reasons["LOCATION_ROUND_ACCURACY"]["value"] == 10.0
    # Un GPS real tiembla y su precisión cambia (aunque a veces sea redonda): nada.
    real = [
        {"latitude": POINT[0], "longitude": POINT[1], "accuracy": 10.0},
        {"latitude": POINT[0] + 0.00001, "longitude": POINT[1], "accuracy": 5.0},
        {"latitude": POINT[0], "longitude": POINT[1] - 0.00002, "accuracy": 8.3},
    ]
    clock(worker["day"], "12:00")
    recorded(punch(client, worker["headers"], "break-start", accuracy=10, samples=real))
    assert not {"LOCATION_STATIC", "LOCATION_ROUND_ACCURACY"} & set(last_reasons())


@pytest.mark.parametrize(
    "samples",
    ["{no es json", json.dumps([{"latitude": 999, "longitude": 0, "accuracy": 1}]), json.dumps([{"latitude": 1}])],
)
def test_invalid_location_samples_are_rejected(client, company_headers, worker, clock, samples):  # noqa: F811
    clock(worker["day"], "07:50")
    response = punch(client, worker["headers"], "check-in", samples=samples)
    assert response.status_code == 422 and response.json()["code"] == "LOCATION_SAMPLES_INVALID"
    assert response.json()["errors"][0]["field"] == "location_samples"


def test_too_many_location_samples_are_rejected(client, company_headers, worker, clock):  # noqa: F811
    clock(worker["day"], "07:50")
    many = [{"latitude": POINT[0], "longitude": POINT[1], "accuracy": 9.0 + i} for i in range(11)]
    response = punch(client, worker["headers"], "check-in", samples=many)
    assert response.status_code == 422 and "10" in response.json()["message"]


def test_moving_faster_than_plausible_between_punches_is_a_location_jump(client, company_headers, clock):  # noqa: F811
    remote = remote_worker(client, company_headers)
    headers, day = remote["headers"], remote["day"]
    clock(day, "07:50")
    recorded(punch(client, headers, "check-in"))
    clock(day, "12:00")  # ~700 km en ~4 h: posible (bajo 200 km/h) pero rápido (sobre la mitad)
    recorded(punch(client, headers, "break-start", at=TIJUANA))
    jump = last_reasons()["LOCATION_JUMP"]
    assert jump["mode"] == "OBSERVE" and jump["threshold"] == 100.0 and 150 < jump["value"] < 200
    clock(day, "12:30")
    recorded(punch(client, headers, "break-end", at=TIJUANA))  # sin moverse: nada
    assert "LOCATION_JUMP" not in last_reasons()


# ---------------------------------------------------------------- 1:N en cada 1:1


def _enroll_as(client, company_headers, person: str, face: str, number: str) -> dict:
    """Un empleado cuyo registro facial es el rostro `face` (p. ej. el de otra persona)."""
    assert create_employee(client, company_headers, number=number, email=f"{person}@empresa.com").status_code == 201
    headers = login(client, f"{person}@empresa.com", "Empleado123")
    enrollment = submit_enrollment(client, headers, frontal=(f"face:{face}".encode(),) * 3, turn_person=face)
    assert enrollment.status_code == 201, enrollment.text
    enrollment_id = enrollment.json()["data"]["enrollment_id"]
    assert client.post(f"/api/enrollments/{enrollment_id}/approve", headers=company_headers).status_code == 200
    return headers


def _verify(client, headers, face: str):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", f"face:{face}".encode(), "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, face) + flash_files(challenge, face)
    return client.post(
        "/api/verification/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
    )


def test_a_face_that_looks_more_like_another_employee(client, company_headers):
    luis = _enroll_as(client, company_headers, "luis", "luis", "EMP-001")
    # Eva quedó registrada con un rostro casi igual al de Luis (fraude interno o un parecido).
    eva = _enroll_as(client, company_headers, "eva", "luis~0.95~eva", "EMP-002")
    assert _verify(client, eva, "luis").json()["data"]["verified"] is True
    mismatch = last_reasons()["IDENTITY_MISMATCH"]
    assert (
        mismatch["mode"] == "OBSERVE"
        and mismatch["threshold"] == 0.0
        and mismatch["value"] == pytest.approx(0.05, abs=1e-3)
    )
    assert _verify(client, luis, "luis").json()["data"]["verified"] is True
    assert "IDENTITY_MISMATCH" not in last_reasons()  # se parece más a sí mismo


def test_the_gallery_is_reused_while_recent_and_never_loaded_if_too_big(client, company_headers, monkeypatch):
    from tests.test_performance import count_queries

    headers = _enroll_as(client, company_headers, "juan", "juan", "EMP-001")
    _verify(client, headers, "juan")
    with count_queries() as cached:
        _verify(client, headers, "juan")
    monkeypatch.setattr("app.core.config.settings.RISK_IDENTITY_GALLERY_MAX_AGE_SECONDS", 0)
    with count_queries() as checked:
        _verify(client, headers, "juan")
    fingerprint = [s for s in checked if "max(" in s.lower() and "face_embeddings" in s]
    assert fingerprint and not [s for s in cached if "max(" in s.lower() and "face_embeddings" in s]
    face_galleries.clear()
    monkeypatch.setattr(face_galleries, "max_bytes", 1)
    _verify(client, headers, "juan")
    assert face_galleries._items == {}  # una galería que no cabe no se carga dentro de un registro
    # Con la señal apagada ni siquiera se lee.
    set_policy(client, company_headers, risk_signals={"IDENTITY_MISMATCH": {"mode": "OFF"}})
    with count_queries() as off:
        _verify(client, headers, "juan")
    assert not [s for s in off if "face_embeddings" in s and "max(" in s.lower()]


# ---------------------------------------------------------------- telemetría del navegador y tablas JPEG


def test_browser_telemetry_is_measured_in_every_face_flow(client, company_headers):
    approved(client, company_headers, "juan", number="EMP-001")
    employee = login(client, "juan@empresa.com", "Empleado123")
    challenge = client.post("/api/face/challenge", headers=employee).json()["data"]
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "juan") + flash_files(challenge, "juan")
    robot = {**GOOD_TELEMETRY, "webdriver": True}
    data = {"challenge_id": challenge["challenge_id"], "telemetry": json.dumps(robot)}
    assert client.post("/api/verification/face", data=data, files=files, headers=employee).json()["data"]["verified"]
    reasons = last_reasons()
    assert reasons["AUTOMATION"]["value"] == 1.0 and reasons["AUTOMATION"]["mode"] == "OBSERVE"
    # Las capturas de prueba no son JPEG: "otro codificador", medido.
    assert reasons["JPEG_TABLE_UNKNOWN"] == {**reasons["JPEG_TABLE_UNKNOWN"], "value": 0.0, "threshold": 92.0}

    # El validador y la empresa en persona también mandan la telemetría de su toma (sin la prueba del dispositivo).
    validator = validator_headers(client, company_headers, mode="FACE")
    assert identify_face(client, validator, "juan").json()["data"]["verified"] is True
    assert last_reasons()["TELEMETRY_MISSING"]["value"] == 0.0 and "DEVICE_KEY_MISSING" not in last_reasons()
    employee_id = client.get("/api/employees", headers=company_headers).json()["data"]["items"][0]["id"]
    challenge = client.post("/api/face/challenge", headers=company_headers).json()["data"]
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "juan") + flash_files(challenge, "juan")
    data = {"challenge_id": challenge["challenge_id"], "telemetry": "{rota"}
    in_person = client.post(
        f"/api/employees/{employee_id}/face/verify", data=data, files=files, headers=company_headers
    )
    assert in_person.json()["data"]["verified"] is True
    assert last_reasons()["TELEMETRY_MISSING"]["value"] == 1.0 and "DEVICE_KEY_MISSING" not in last_reasons()
    assert len(assessments()) == 3


# ---------------------------------------------------------------- simulación y caso de fraude


def test_the_simulation_replays_the_device_mode(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    company_id = company_id_of(client, company_headers)
    from app.services.risk_rules import Hit

    with SessionLocal() as db:
        db.add_all([_assessment(company_id, [Hit("DEVICE_NEW", 0.0)]), _assessment(company_id, [])])
        db.commit()
    result = client.post(f"{url}/simulate", json={"employee_device_mode": "APPROVAL"}, headers=admin).json()["data"]
    assert result["current"]["allow"] == 2 and result["candidate"]["review"] == 1 and result["stricter"] == 1
    bad = client.post(f"{url}/simulate", json={"employee_device_mode": "NOPE"}, headers=admin)
    assert bad.status_code == 422 and bad.json()["code"] == "INVALID_DEVICE_MODE"
    # Cambiar el modo guarda su simulación en el historial (decide como el motor).
    change = client.put(url, json={"employee_device_mode": "STEP_UP"}, headers=admin).json()["data"]["change"]
    assert change["simulation"]["candidate"]["step_up"] == 1


def test_a_fraud_case_explains_each_signal_and_its_network(client, company_headers):
    approved(client, company_headers, "juan", number="EMP-001")
    headers = login(client, "juan@empresa.com", "Empleado123")
    set_policy(
        client,
        company_headers,
        risk_medium_action="ALERT",
        risk_signals={"NETWORK_HOSTING": {"mode": "ENFORCE", "points": 40}},
    )
    install()
    with from_ip(US_CLOUD) as cloud:
        challenge = cloud.post("/api/face/challenge", headers=headers).json()["data"]
        files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(2)]
        files += turn_files(challenge, "juan") + flash_files(challenge, "juan")
        response = cloud.post(
            "/api/verification/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
        )
    assert response.json()["data"]["verified"] is True and assessments()[-1].action == "ALERT"
    from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    admin = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    case = client.get("/api/admin/fraud-cases", headers=admin).json()["data"]["items"][0]
    assert case["kind"] == "INJECTION"
    detail = client.get(f"/api/admin/fraud-cases/{case['id']}", headers=admin).json()["data"]
    [attempt] = detail["attempts_detail"]
    hosting = next(s for s in attempt["signals"] if s["code"] == "NETWORK_HOSTING")
    assert hosting["name"] == "Red de un centro de datos" and hosting["description"].startswith("La IP es de una nube")
    assert attempt["network"] == {"country": "US", "asn": 16509, "organization": "Amazon.com, Inc.", "hosting": True}
    english = client.get(f"/api/admin/fraud-cases/{case['id']}", headers={**admin, "Accept-Language": "en-US"}).json()
    signal = next(s for s in english["data"]["attempts_detail"][0]["signals"] if s["code"] == "NETWORK_HOSTING")
    assert signal["name"] == "Data center network"
