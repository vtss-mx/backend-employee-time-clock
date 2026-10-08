"""Validadores de identidad (rol VALIDATOR): la empresa los administra; identifican a sus
empleados por QR, por rostro (1:N), por cualquiera de los dos o por ambos."""

import pytest

from app.core.database import SessionLocal
from app.models import VerificationLog
from app.services.face_gallery import face_galleries
from tests.conftest import (
    DESKTOP_UA,
    create_company,
    create_employee,
    login,
    qr_content,
    submit_enrollment,
    turn_files,
)
from tests.test_policy import set_policy

URL = "/api/validators"
TABLET_UA = "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
PASSWORD = "Validador123"


@pytest.fixture(autouse=True)
def _fresh_gallery():
    face_galleries.clear()  # cada prueba usa una BD nueva
    yield


#: Domicilio de un acceso (Plaza Zaragoza, Hermosillo) con su punto en el mapa.
ADDRESS = {
    "street": "Calle Dr. Paliza",
    "exterior_number": "71",
    "interior_number": None,
    "postal_code": "83000",
    "country_code": "MX",
    "state": "Sonora",
    "municipality": "Hermosillo",
    "city": "Hermosillo",
    "neighborhood": "Centro",
    "latitude": 29.0729,
    "longitude": -110.9559,
}


def create_validator(
    client, company_headers, *, mode="QR_OR_FACE", email="recepcion@empresa.com", name="Recepción", **extra
):
    body = {"name": name, "email": email, "password": PASSWORD, "mode": mode, "address": ADDRESS, **extra}
    return client.post(URL, json=body, headers=company_headers)


def validator_headers(client, company_headers, **kwargs) -> dict[str, str]:
    assert create_validator(client, company_headers, **kwargs).status_code == 201
    return login(client, kwargs.get("email", "recepcion@empresa.com"), PASSWORD)


def approved(client, company_headers, person: str, *, number: str, **kwargs) -> dict:
    """Empleado con su rostro (`person`) registrado y aprobado por la empresa."""
    employee = create_employee(client, company_headers, number=number, email=f"{person}@empresa.com", **kwargs)
    assert employee.status_code == 201, employee.text
    headers = login(client, f"{person}@empresa.com", "Empleado123")
    enrollment = submit_enrollment(client, headers, frontal=(f"face:{person}".encode(),) * 3, turn_person=person)
    assert enrollment.status_code == 201, enrollment.text
    approve = client.post(
        f"/api/enrollments/{enrollment.json()['data']['enrollment_id']}/approve", headers=company_headers
    )
    assert approve.status_code == 200
    return employee.json()["data"]


def identify_face(
    client, headers, person: str, *, kind: str = "face", turn_person: str | None = None, qr: str | None = None
):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"c{i}.jpg", f"{kind}:{person}".encode(), "image/jpeg")) for i in range(3)]
    files += turn_files(challenge, turn_person or person)
    data = {"challenge_id": challenge["challenge_id"], **({"qr_content": qr} if qr else {})}
    return client.post("/api/checkpoint/identify/face", data=data, files=files, headers=headers)


# ---------------------------------------------------------------- administración (COMPANY)


def test_company_manages_its_validators(client, company_headers, admin_headers):
    created = create_validator(client, company_headers, mode="QR")
    assert created.status_code == 201
    validator = created.json()["data"]
    assert validator["mode"] == "QR" and validator["active"] is True and validator["identifications_today"] == 0
    duplicate = create_validator(client, company_headers)
    assert duplicate.status_code == 409 and duplicate.json()["errors"][0]["field"] == "email"
    weak = client.post(URL, json={"name": "X", "email": "x@empresa.com", "password": "corta"}, headers=company_headers)
    assert weak.status_code == 422

    url = f"{URL}/{validator['id']}"
    updated = client.put(url, json={"mode": "QR_AND_FACE", "name": "  Acceso   norte "}, headers=company_headers)
    assert updated.json()["data"]["mode"] == "QR_AND_FACE" and updated.json()["data"]["name"] == "Acceso norte"
    assert [v["name"] for v in client.get(URL, headers=company_headers).json()["data"]["items"]] == ["Acceso norte"]

    # El validador entra; desactivarlo cierra su sesión; restablecer su contraseña también.
    headers = login(client, "recepcion@empresa.com", PASSWORD)
    assert client.get("/api/checkpoint/me", headers=headers).json()["data"]["mode"] == "QR_AND_FACE"
    client.patch(f"{url}/status", json={"active": False}, headers=company_headers)
    assert client.get("/api/checkpoint/me", headers=headers).status_code == 401
    client.patch(f"{url}/status", json={"active": True}, headers=company_headers)
    client.put(f"{url}/password", json={"password": "Nueva12345"}, headers=company_headers)
    assert login(client, "recepcion@empresa.com", "Nueva12345")

    # Otra empresa no lo ve; un validador no administra validadores.
    assert client.get(url, headers=company_headers).json()["data"]["address"]["city"] == "Hermosillo"
    create_company(client, admin_headers, max_validators=1)  # con el módulo: el 404 es por ser de otra empresa
    other = login(client, "admin@panificadora.com", "Empresa1234")
    assert client.get(url, headers=other).status_code == 404
    assert client.patch(f"{url}/status", json={"active": False}, headers=other).status_code == 404
    assert client.get(URL, headers=login(client, "recepcion@empresa.com", "Nueva12345")).status_code == 403

    assert client.delete(url, headers=company_headers).status_code == 200
    assert (
        client.post("/api/auth/login", json={"email": "recepcion@empresa.com", "password": "Nueva12345"}).status_code
        == 401
    )


def test_validators_only_from_tablets_or_phones(client, company_headers):
    set_policy(client, company_headers, validator_device_approval=False)  # aquí solo el tipo de dispositivo
    create_validator(client, company_headers)
    credentials = {"email": "recepcion@empresa.com", "password": PASSWORD}
    desktop = client.post("/api/auth/login", json=credentials, headers={"User-Agent": DESKTOP_UA})
    assert desktop.status_code == 403 and desktop.json()["code"] == "TOUCH_DEVICE_REQUIRED"
    assert client.post("/api/auth/login", json=credentials, headers={"User-Agent": TABLET_UA}).status_code == 200
    assert client.post("/api/auth/login", json=credentials).status_code == 200  # teléfono
    # La empresa puede permitir computadoras (política en la BD).
    set_policy(client, company_headers, validator_mobile_only=False)
    assert client.post("/api/auth/login", json=credentials, headers={"User-Agent": DESKTOP_UA}).status_code == 200


# ---------------------------------------------------------------- identificación por QR


def test_identify_by_qr(client, company_headers, admin_headers):
    juan = create_employee(client, company_headers).json()["data"]
    headers = validator_headers(client, company_headers, mode="QR")
    content = qr_content(juan["id"])
    ok = client.post("/api/checkpoint/identify/qr", json={"qr_content": content}, headers=headers)
    body = ok.json()
    assert body["code"] == "EMPLOYEE_IDENTIFIED" and body["data"]["name"] == "Juan Pérez"
    # Un solo uso: el mismo QR ya no vuelve a servir.
    again = client.post("/api/checkpoint/identify/qr", json={"qr_content": content}, headers=headers).json()["data"]
    assert again["verified"] is False and "ya se usó" in again["message"]

    # QR de otra empresa: "no reconocido" (no revela que existe en otra empresa).
    create_company(client, admin_headers)
    other = login(client, "admin@panificadora.com", "Empresa1234")
    stranger = create_employee(client, other, number="PAN-1", email="ana@pan.com", phone="+52 662 765 4321")
    foreign = client.post(
        "/api/checkpoint/identify/qr", json={"qr_content": qr_content(stranger.json()["data"]["id"])}, headers=headers
    )
    assert foreign.json()["data"] == {**foreign.json()["data"], "verified": False, "message": "QR no reconocido"}

    client.patch(f"/api/employees/{juan['id']}/status", json={"active": False}, headers=company_headers)
    inactive = client.post("/api/checkpoint/identify/qr", json={"qr_content": qr_content(juan["id"])}, headers=headers)
    assert inactive.json()["data"]["message"] == "El empleado está desactivado"

    # Modo QR: el rostro no está permitido (ni el reto de prueba de vida ni la identificación).
    challenge = client.post("/api/face/challenge", headers=headers)
    assert challenge.status_code == 409 and challenge.json()["code"] == "VALIDATOR_METHOD_NOT_ALLOWED"
    files = [("images", ("c.jpg", b"face:juan", "image/jpeg"))]
    denied = client.post("/api/checkpoint/identify/face", files=files, headers=headers)
    assert denied.status_code == 409 and denied.json()["code"] == "VALIDATOR_METHOD_NOT_ALLOWED"


# ---------------------------------------------------------------- identificación por rostro (1:N)


def test_identify_by_face_among_all_employees(client, company_headers):
    approved(client, company_headers, "juan", number="EMP-001")
    ana = approved(client, company_headers, "ana", number="EMP-002")
    headers = validator_headers(client, company_headers, mode="FACE")

    found = identify_face(client, headers, "ana")
    assert found.status_code == 200, found.text
    assert found.json()["data"]["verified"] is True and found.json()["data"]["employee_id"] == ana["id"]

    unknown = identify_face(client, headers, "pedro")  # no es empleado de la empresa
    assert unknown.json()["data"]["verified"] is False and unknown.json()["code"] == "EMPLOYEE_NOT_IDENTIFIED"
    assert identify_face(client, headers, "ana", kind="mask").status_code == 422  # cubrebocas: no se identifica
    assert identify_face(client, headers, "ana", kind="glasses").json()["data"]["verified"] is True  # lentes: sí
    liveness = identify_face(client, headers, "ana", turn_person="juan")  # el giro es de otra persona
    assert liveness.json()["data"]["verified"] is False

    # La bitácora central registra al validador como autor de cada intento.
    with SessionLocal() as db:
        logs = db.query(VerificationLog).order_by(VerificationLog.id).all()
        mine = [log for log in logs if log.method.value == "FACE" and log.employee_id in (ana["id"], None)]
        assert {log.reason for log in mine} >= {None, "NO_MATCH", "LIVENESS_MISMATCH"}

    page = client.get("/api/checkpoint/recent", headers=headers).json()["data"]
    recent = page["items"]
    assert page["total"] == 4 and (page["page"], page["size"]) == (1, 10)
    assert [event["success"] for event in recent] == [False, True, False, True]  # los más recientes primero
    second = client.get("/api/checkpoint/recent", params={"size": 2, "page": 2}, headers=headers).json()["data"]
    assert [event["id"] for event in second["items"]] == [recent[2]["id"], recent[3]["id"]]
    assert recent[3]["employee_number"] == "EMP-002" and recent[0]["employee_name"] is None
    listed = client.get(URL, headers=company_headers).json()["data"]["items"][0]
    assert listed["identifications_today"] == 2


def test_headwear_is_decided_after_knowing_who_it_is(client, company_headers):
    approved(client, company_headers, "juan", number="EMP-001")
    approved(client, company_headers, "sara", number="EMP-002")
    sara = client.get("/api/employees", params={"search": "sara"}, headers=company_headers).json()["data"]["items"][0]
    client.put(f"/api/employees/{sara['id']}", json={"headwear_exempt": True}, headers=company_headers)
    headers = validator_headers(client, company_headers, mode="FACE")

    assert identify_face(client, headers, "juan", kind="hat").status_code == 422  # sin excepción
    exempt = identify_face(client, headers, "sara", kind="hat")  # con excepción registrada
    assert exempt.status_code == 200 and exempt.json()["data"]["verified"] is True


def test_two_people_with_the_same_face_are_not_guessed(client, company_headers):
    approved(client, company_headers, "gemelo", number="EMP-001")
    create_employee(client, company_headers, number="EMP-002", email="otro@empresa.com", phone="+52 662 765 4321")
    other = login(client, "otro@empresa.com", "Empleado123")
    enrollment = submit_enrollment(client, other, frontal=(b"face:gemelo",) * 3, turn_person="gemelo")
    client.post(f"/api/enrollments/{enrollment.json()['data']['enrollment_id']}/approve", headers=company_headers)
    headers = validator_headers(client, company_headers, mode="FACE")
    result = identify_face(client, headers, "gemelo").json()["data"]
    assert result["verified"] is False and "certeza" in result["message"]


# ---------------------------------------------------------------- QR y rostro (ambos)


def test_identify_with_qr_and_face(client, company_headers):
    juan = approved(client, company_headers, "juan", number="EMP-001")
    ana = approved(client, company_headers, "ana", number="EMP-002")
    headers = validator_headers(client, company_headers, mode="QR_AND_FACE")
    qr = qr_content(ana["id"])

    holder = client.post("/api/checkpoint/qr/inspect", json={"qr_content": qr}, headers=headers)
    assert holder.json()["data"]["name"] == ana["full_name"]
    # Escanearlo lo usó: ni otra lectura ni otro validador pueden aprovecharlo.
    again = client.post("/api/checkpoint/qr/inspect", json={"qr_content": qr}, headers=headers)
    assert again.status_code == 422 and again.json()["code"] == "QR_ALREADY_USED"
    assert client.post("/api/checkpoint/identify/qr", json={"qr_content": qr}, headers=headers).status_code == 409
    assert identify_face(client, headers, "ana").json()["code"] == "QR_REQUIRED"  # sin QR no basta el rostro

    both = identify_face(client, headers, "ana", qr=qr).json()["data"]
    assert both["verified"] is True and both["method"] == "QR_FACE"
    reused = identify_face(client, headers, "ana", qr=qr).json()["data"]  # el rostro lo cerró: no se repite
    assert reused["verified"] is False and "ya se usó" in reused["message"]

    impostor_qr = qr_content(ana["id"])
    client.post("/api/checkpoint/qr/inspect", json={"qr_content": impostor_qr}, headers=headers)
    impostor = identify_face(client, headers, "juan", qr=impostor_qr).json()["data"]  # QR de ana, rostro de juan
    assert impostor["verified"] is False and "dueño del código QR" in impostor["message"]
    assert juan["id"] != ana["id"]


def test_company_deactivation_closes_validator_sessions(client, company_headers, admin_headers):
    headers = validator_headers(client, company_headers)
    company_id = client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]
    client.patch(f"/api/admin/companies/{company_id}/status", json={"active": False}, headers=admin_headers)
    assert client.get("/api/checkpoint/me", headers=headers).status_code == 401
    denied = client.post("/api/auth/login", json={"email": "recepcion@empresa.com", "password": PASSWORD})
    assert denied.json()["code"] == "COMPANY_INACTIVE"


def test_identifying_among_everyone_can_demand_more_than_verifying_one(client, company_headers):
    """La confianza 1:N (identificar entre toda la plantilla) se configura aparte y nunca baja de la 1:1."""
    from app.core.config import settings
    from app.facial_recognition.calibration import similarity_for_confidence
    from tests.test_policy import set_policy

    ana = approved(client, company_headers, "ana", number="EMP-002")
    headers = validator_headers(client, company_headers, mode="FACE")
    model = settings.FACE_RECOGNITION_MODEL
    # Una captura con confianza entre 99 % y 99.999 %: basta con 99 %, no con 99.999 %.
    middle = (similarity_for_confidence(0.99, model) + similarity_for_confidence(0.99999, model)) / 2
    person = f"ana~{middle:.6f}~luz"
    # Sin aprendizaje continuo: lo aprendido de la primera identificación no debe ayudar a las demás.
    set_policy(client, company_headers, min_confidence=0.99, identify_confidence=0.99, adaptive_learning=False)
    assert identify_face(client, headers, person).json()["data"]["employee_id"] == ana["id"]
    set_policy(client, company_headers, identify_confidence=0.99999)  # más estricto solo al identificar
    assert identify_face(client, headers, person).json()["data"]["verified"] is False
    # Si la 1:N quedara por debajo de la 1:1, rige la 1:1.
    set_policy(client, company_headers, min_confidence=0.99999, identify_confidence=0.99)
    assert identify_face(client, headers, person).json()["data"]["verified"] is False
