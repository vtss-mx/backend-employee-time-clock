"""Dispositivo del empleado (antifraude 1b, decisión D2): la llave no exportable de su navegador firma el reto que llega
con el de la prueba de vida; el servidor guarda solo su hash. Modos de la empresa: apagado, solo medir (señales), un
paso más ante uno desconocido y aprobación de la empresa (sus registros quedan "en revisión" hasta aprobarlo). La
empresa ve, aprueba y revoca los dispositivos en la ficha del empleado; el empleado ve los suyos en Mi perfil."""

from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, update

from app.core.database import SessionLocal
from app.i18n import stored
from app.models import Employee, EmployeeDevice, VerificationPolicy, WorkSession
from app.services.device_service import issue_nonce
from app.services.policy_service import clear_policy_cache
from tests.conftest import DESKTOP_UA, TEST_DEVICE_KEY, approved_employee, device_proof, flash_files, login, turn_files
from tests.test_attendance import clock, recorded, worker  # noqa: F401 (fixtures)
from tests.test_policy import set_policy
from tests.test_risk_engine import assessments, last_reasons
from tests.test_shifts import POINT
from tests.test_validators import approved

VERIFY = "/api/verification/face"
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


def signed(nonce: str | None, key: ec.EllipticCurvePrivateKey = TEST_DEVICE_KEY) -> dict:
    """La prueba del dispositivo como la manda la app (llave pública, reto y firma)."""
    if nonce is None:
        return {}
    proof = device_proof(nonce, key)
    return {"device_key": proof["public_key"], "device_nonce": nonce, "device_signature": proof["signature"]}


def verify(client, headers, *, key=TEST_DEVICE_KEY, person="juan", challenge=None, proof: dict | None = None):
    """Verificación 1:1 firmando el reto del dispositivo (salvo `proof`, que la reemplaza)."""
    challenge = challenge or client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", f"face:{person}".encode(), "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, person) + flash_files(challenge, person)
    data = {
        "challenge_id": challenge["challenge_id"],
        "camera_label": "Cámara frontal",
        **(signed(challenge.get("device_nonce"), key) if proof is None else proof),
    }
    return client.post(VERIFY, data=data, files=files, headers=headers)


def device_mode(client, company_headers, mode: str) -> None:
    set_policy(client, company_headers, employee_device_mode=mode)


def devices() -> list[EmployeeDevice]:
    with SessionLocal() as db:
        return list(db.scalars(select(EmployeeDevice).order_by(EmployeeDevice.id)))


def employee_id(client, company_headers, email="juan@empresa.com") -> int:
    found = client.get("/api/employees", params={"search": email}, headers=company_headers).json()["data"]["items"]
    return found[0]["id"]


# ---------------------------------------------------------------- reto y solo medir (por omisión)


def test_only_the_employee_gets_a_device_nonce_and_only_if_the_company_binds_devices(client, company_headers):
    headers = approved_employee(client, company_headers)
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert challenge["device_nonce"] and challenge["liveness_required"] is True
    # La empresa (en persona) y el validador no firman con un dispositivo del empleado.
    assert client.post("/api/face/challenge", headers=company_headers).json()["data"]["device_nonce"] is None
    device_mode(client, company_headers, "OFF")
    assert client.post("/api/face/challenge", headers=headers).json()["data"]["device_nonce"] is None
    # Sin prueba de vida también llega (el registro se firma igual).
    device_mode(client, company_headers, "OBSERVE")
    set_policy(client, company_headers, liveness_challenge=False)
    plain = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert plain["liveness_required"] is False and plain["device_nonce"]


def test_observe_mode_measures_a_new_device_once_and_remembers_only_its_hash(client, company_headers):
    headers = approved_employee(client, company_headers)
    first = verify(client, headers)
    assert first.json()["data"]["verified"] is True
    assert last_reasons()["DEVICE_NEW"] == {**last_reasons()["DEVICE_NEW"], "mode": "OBSERVE", "value": 0.0}
    [device] = devices()
    assert len(device.key_hash) == 64 and device.status == "PENDING" and device.uses == 1
    assert device.name == "iPhone · Safari"  # del navegador (la llave nunca se guarda)
    assert verify(client, headers).json()["data"]["verified"] is True
    assert "DEVICE_NEW" not in last_reasons()  # ya lo conoce
    [device] = devices()
    assert device.uses == 2 and device.last_seen_at >= device.first_seen_at


def test_a_missing_or_invalid_proof_is_a_signal_never_a_rejection(client, company_headers):
    headers = approved_employee(client, company_headers)
    assert verify(client, headers, proof={}).json()["data"]["verified"] is True
    assert last_reasons()["DEVICE_KEY_MISSING"]["value"] == 0.0
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    forged = {**signed(challenge["device_nonce"]), "device_signature": signed(issue_nonce(999))["device_signature"]}
    assert verify(client, headers, challenge=challenge, proof=forged).json()["data"]["verified"] is True
    assert last_reasons()["DEVICE_KEY_MISSING"]["value"] == 1.0
    # El reto de OTRA cuenta (bien firmado) tampoco sirve, ni valores desmedidos.
    stolen = signed(issue_nonce(999))
    assert verify(client, headers, proof=stolen).json()["data"]["verified"] is True
    assert last_reasons()["DEVICE_KEY_MISSING"]["value"] == 1.0
    huge = {**stolen, "device_key": "A" * 400}
    assert verify(client, headers, proof=huge).json()["data"]["verified"] is True
    assert devices() == []


def test_with_the_mode_off_nothing_is_bound(client, company_headers):
    headers = approved_employee(client, company_headers)
    device_mode(client, company_headers, "OFF")
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    response = verify(client, headers, challenge=challenge, proof=signed(issue_nonce(1)))
    assert response.json()["data"]["verified"] is True
    assert not {"DEVICE_NEW", "DEVICE_KEY_MISSING"} & set(last_reasons()) and devices() == []


def test_a_device_used_by_several_employees_is_shared(client, company_headers):
    juan = approved_employee(client, company_headers)
    approved(client, company_headers, "ana", number="EMP-002")
    ana = login(client, "ana@empresa.com", "Empleado123")
    assert verify(client, juan).json()["data"]["verified"] is True
    assert "DEVICE_SHARED" not in last_reasons()
    assert verify(client, ana, person="ana").json()["data"]["verified"] is True  # el MISMO teléfono
    assert last_reasons()["DEVICE_SHARED"] == {**last_reasons()["DEVICE_SHARED"], "value": 2.0, "threshold": 2.0}
    # Fuera de la ventana ya no cuenta.
    with SessionLocal() as db:
        db.execute(update(EmployeeDevice).values(last_seen_at=datetime.now(UTC) - timedelta(days=1)))
        db.commit()
    assert verify(client, ana, person="ana").json()["data"]["verified"] is True
    assert "DEVICE_SHARED" not in last_reasons()


# ---------------------------------------------------------------- un paso más en un dispositivo nuevo


def test_step_up_mode_asks_once_per_device_and_then_trusts_it(client, company_headers):
    headers = approved_employee(client, company_headers)
    device_mode(client, company_headers, "STEP_UP")
    asked = verify(client, headers)
    assert asked.status_code == 422 and asked.json()["code"] == "STEP_UP_REQUIRED"
    step_up = asked.json()["errors"][0]["details"]["challenge"]
    assert step_up["step_up"] is True and step_up["device_nonce"]  # el reto nuevo también se firma
    assert assessments()[-1].action == "STEP_UP" and devices()[0].stepped_up_at is None
    passed = verify(client, headers, challenge=step_up)
    assert passed.json()["data"]["verified"] is True
    assert devices()[0].stepped_up_at is not None
    again = verify(client, headers)
    assert again.json()["data"]["verified"] is True and "DEVICE_NEW" not in last_reasons()
    # Otro teléfono vuelve a pedirlo; sin llave, cada vez.
    assert verify(client, headers, key=OTHER_KEY).json()["code"] == "STEP_UP_REQUIRED"
    assert verify(client, headers, proof={}).json()["code"] == "STEP_UP_REQUIRED"


def test_without_liveness_an_unknown_device_goes_to_review_instead(client, company_headers, worker, clock):  # noqa: F811
    device_mode(client, company_headers, "STEP_UP")
    set_policy(client, company_headers, liveness_challenge=False)
    clock(worker["day"], "07:50")
    files = [("images", (f"c{i}.jpg", b"face:ana", "image/jpeg")) for i in range(2)]
    data = {"latitude": POINT[0], "longitude": POINT[1], "accuracy": 12}
    session = recorded(client.post("/api/me/attendance/check-in", data=data, files=files, headers=worker["headers"]))
    assert session["review_status"] == "PENDING"


# ---------------------------------------------------------------- aprobación de la empresa


def test_approval_mode_sends_records_to_review_until_the_company_approves_the_device(
    client,
    company_headers,
    worker,  # noqa: F811
    clock,  # noqa: F811
):
    device_mode(client, company_headers, "APPROVAL")
    headers = worker["headers"]
    clock(worker["day"], "07:50")
    session = recorded(act_signed(client, headers, "check-in"))
    assert session["review_status"] == "PENDING"
    reviewed = client.get(f"/api/attendance/sessions/{session['id']}", headers=company_headers).json()["data"]
    assert reviewed["review_reasons"] == ["DEVICE"] and session["review_reasons"] == []  # el empleado no ve el motivo

    url = f"/api/employees/{worker['id']}/devices"
    listed = client.get(url, headers=company_headers)
    assert listed.status_code == 200 and listed.json()["code"] == "EMPLOYEE_DEVICES_LISTED"
    [device] = listed.json()["data"]["items"]
    assert device["status"] == "PENDING" and device["name"] == "iPhone · Safari" and device["uses"] == 1
    approved_device = client.patch(f"{url}/{device['id']}/status", json={"status": "APPROVED"}, headers=company_headers)
    assert approved_device.status_code == 200 and approved_device.json()["data"]["status"] == "APPROVED"
    assert approved_device.json()["message"].startswith("Dispositivo aprobado")
    assert approved_device.json()["data"]["reviewed_by"] == "admin@empresa.com"

    with SessionLocal() as db:  # la jornada sigue en revisión: la decide la empresa (D10)
        db.execute(update(WorkSession).values(review_status=None))
        db.commit()
    clock(worker["day"], "12:00")
    later = recorded(act_signed(client, headers, "break-start"))
    assert later["review_status"] is None

    revoked = client.patch(f"{url}/{device['id']}/status", json={"status": "REVOKED"}, headers=company_headers)
    assert revoked.json()["data"]["status"] == "REVOKED" and revoked.json()["message"].startswith(
        "Dispositivo revocado"
    )
    clock(worker["day"], "12:30")  # revocado vuelve a ser desconocido: su registro queda en revisión
    assert recorded(act_signed(client, headers, "break-end"))["review_status"] == "PENDING"
    again = client.patch(f"{url}/{device['id']}/status", json={"status": "REVOKED"}, headers=company_headers)
    assert again.status_code == 409 and again.json()["code"] == "DEVICE_INVALID_TRANSITION"
    missing = client.patch(f"{url}/999/status", json={"status": "APPROVED"}, headers=company_headers)
    assert missing.status_code == 404 and missing.json()["code"] == "DEVICE_NOT_FOUND"
    wrong = client.patch(f"{url}/{device['id']}/status", json={"status": "REJECTED"}, headers=company_headers)
    assert wrong.status_code == 422


def test_the_engine_off_still_applies_the_device_mode(client, company_headers, worker, clock):  # noqa: F811
    with SessionLocal() as db:
        db.execute(update(VerificationPolicy).values(risk_engine=False, employee_device_mode="APPROVAL"))
        db.commit()
    clear_policy_cache()
    clock(worker["day"], "07:50")
    assert recorded(act_signed(client, worker["headers"], "check-in"))["review_status"] == "PENDING"


def test_the_employee_sees_their_own_devices_in_their_profile(client, company_headers):
    headers = approved_employee(client, company_headers)
    empty = client.get("/api/users/me/devices", headers=headers)
    assert empty.status_code == 200 and empty.json()["data"]["total"] == 0
    verify(client, headers)
    verify(client, headers, key=OTHER_KEY)
    mine = client.get("/api/users/me/devices", headers=headers).json()
    assert mine["code"] == "MY_DEVICES" and mine["data"]["total"] == 2 and mine["message"] == "2 dispositivos"
    assert all(item["reviewed_by"] is None for item in mine["data"]["items"])
    # En inglés, el nombre de un navegador que no se reconoce sale en el idioma de quien lee.
    english = {**headers, "Accept-Language": "en-US", "User-Agent": "curl/8.0"}
    verify(client, english, key=ec.generate_private_key(ec.SECP256R1()))
    names = [item["name"] for item in client.get("/api/users/me/devices", headers=english).json()["data"]["items"]]
    assert names[0] == "Computer" and "iPhone · Safari" in names


def test_device_routes_belong_to_their_roles(client, company_headers):
    headers = approved_employee(client, company_headers)
    target = employee_id(client, company_headers)
    assert client.get(f"/api/employees/{target}/devices", headers=headers).status_code == 403
    assert client.get("/api/users/me/devices", headers=company_headers).status_code == 403
    assert client.get("/api/employees/9999/devices", headers=company_headers).status_code == 404


def test_a_device_without_a_recognizable_browser_is_named_by_its_kind(client, company_headers):
    headers = {**approved_employee(client, company_headers), "User-Agent": "Robot/1.0"}
    verify(client, headers)
    assert devices()[0].name == stored("DEVICE_DESKTOP")  # la llave del texto: se traduce al leerse
    verify(client, {**headers, "User-Agent": DESKTOP_UA}, key=OTHER_KEY)
    assert devices()[1].name == "Mac · Chrome"


def act_signed(client, headers, action: str, *, challenge: dict | None = None):
    """Un registro de asistencia firmando el reto del dispositivo."""
    challenge = challenge or client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"c{i}.jpg", b"face:ana", "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "ana")
    data = {
        "challenge_id": challenge["challenge_id"],
        "latitude": POINT[0],
        "longitude": POINT[1],
        "accuracy": 12,
        **signed(challenge["device_nonce"]),
    }
    return client.post(f"/api/me/attendance/{action}", data=data, files=files, headers=headers)


def test_a_device_of_another_company_is_never_found(client, company_headers, admin_headers):
    """Aislamiento: el dispositivo de un empleado de otra empresa no se ve ni se cambia, ni por su empleado ni
    combinándolo con uno propio (la fila se busca con la empresa de la sesión; la base también la filtra)."""
    from tests.conftest import create_company, create_employee

    ana = approved_employee(client, company_headers, email="ana@empresa.com", number="A-1")
    verify(client, ana)
    assert create_company(client, admin_headers).status_code == 201
    other = login(client, "admin@panificadora.com", "Empresa1234")
    assert create_employee(client, other, number="B-1", email="b1@panificadora.com").status_code == 201
    b_employee = employee_id(client, other, "b1@panificadora.com")
    [device] = devices()
    with SessionLocal() as db:
        db.add(
            EmployeeDevice(
                company_id=db.get(Employee, b_employee).company_id,
                employee_id=b_employee,
                key_hash="b" * 64,
                name="Android · Chrome",
                last_seen_at=datetime.now(UTC),
            )
        )
        db.commit()
    b_device = devices()[1].id
    a_employee = employee_id(client, company_headers, "ana@empresa.com")
    assert client.get(f"/api/employees/{b_employee}/devices", headers=company_headers).status_code == 404
    crossed = client.patch(
        f"/api/employees/{a_employee}/devices/{b_device}/status", json={"status": "APPROVED"}, headers=company_headers
    )
    assert crossed.status_code == 404 and crossed.json()["code"] == "DEVICE_NOT_FOUND"
    mine = client.get(f"/api/employees/{a_employee}/devices", headers=company_headers).json()["data"]["items"]
    assert [item["id"] for item in mine] == [device.id]
