"""La empresa con el empleado presente: registra su rostro (aprobado al momento) y verifica su
identidad 1:1 con su propia cámara."""

from tests.conftest import create_company, create_employee, enrollment_challenge, login, turn_files
from tests.test_validators import validator_headers


def in_person(
    client, headers, employee_id: int, action: str, person: str = "juan", *, frames: int = 3, turn_person=None
):
    """Captura en persona: reto de la empresa (el del registro, con los cuatro movimientos, al registrar), capturas
    frontales y las de los movimientos."""
    if action == "enroll":
        challenge = enrollment_challenge(client, headers)
    else:
        challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", f"face:{person}".encode(), "image/jpeg")) for i in range(frames)]
    files += turn_files(challenge, turn_person or person)
    return client.post(
        f"/api/employees/{employee_id}/face/{action}",
        data={"challenge_id": challenge["challenge_id"]},
        files=files,
        headers=headers,
    )


def _employee(client, company_headers) -> dict:
    created = create_employee(client, company_headers)
    assert created.status_code == 201, created.text
    return created.json()["data"]


def test_company_enrolls_the_face_in_person_and_it_is_approved_at_once(client, company_headers):
    employee = _employee(client, company_headers)
    challenge = client.post("/api/face/challenge", headers=company_headers)
    assert challenge.status_code == 200 and challenge.json()["data"]["liveness_required"] is True

    enrolled = in_person(client, company_headers, employee["id"], "enroll", frames=5)
    assert enrolled.status_code == 201, enrolled.text
    body = enrolled.json()
    assert body["code"] == "FACE_ENROLLED_IN_PERSON" and body["data"]["face_status"] == "APPROVED"

    # Queda constancia de quién lo capturó y quién lo aprobó (la misma persona, en persona).
    listed = client.get("/api/enrollments", params={"status": "APPROVED"}, headers=company_headers).json()["data"]
    assert listed["items"][0]["captured_by"] == "admin@empresa.com"
    assert listed["items"][0]["reviewed_by"] == "admin@empresa.com"
    assert (
        client.get(f"/api/employees/{employee['id']}", headers=company_headers).json()["data"]["face_status"]
        == "APPROVED"
    )

    # El empleado ya puede identificarse con su rostro (no pasa por la bandeja de validaciones).
    me = client.get("/api/users/me", headers=login(client, "juan@empresa.com", "Empleado123")).json()["data"]
    assert me["employee"]["face_status"] == "APPROVED"


def test_company_verifies_the_identity_in_person(client, company_headers):
    employee = _employee(client, company_headers)
    not_yet = in_person(client, company_headers, employee["id"], "verify")
    assert not_yet.status_code == 409 and not_yet.json()["code"] == "FACE_NOT_APPROVED"

    in_person(client, company_headers, employee["id"], "enroll")
    same = in_person(client, company_headers, employee["id"], "verify")
    assert same.status_code == 200 and same.json()["code"] == "IDENTITY_VERIFIED"
    assert same.json()["data"]["verified"] is True and same.json()["data"]["name"] == employee["full_name"]

    other = in_person(client, company_headers, employee["id"], "verify", person="pedro")
    assert other.status_code == 200 and other.json()["code"] == "IDENTITY_NOT_VERIFIED"
    assert other.json()["data"]["verified"] is False

    # Cada intento queda en la bitácora del empleado.
    history = client.get(f"/api/employees/{employee['id']}/verifications", headers=company_headers).json()["data"][
        "items"
    ]
    assert [(h["method"], h["success"]) for h in history[:2]] == [("FACE", False), ("FACE", True)]


def test_in_person_capture_needs_liveness_from_the_same_person(client, company_headers):
    employee = _employee(client, company_headers)
    spoofed = in_person(client, company_headers, employee["id"], "enroll", turn_person="pedro")
    assert spoofed.status_code == 422  # el giro es de otra persona: no se registra
    assert (
        client.get(f"/api/employees/{employee['id']}", headers=company_headers).json()["data"]["face_status"]
        == "NOT_ENROLLED"
    )


def test_only_the_company_of_the_employee_captures_in_person(client, company_headers, admin_headers):
    employee = _employee(client, company_headers)
    create_company(client, admin_headers)
    other_company = login(client, "admin@panificadora.com", "Empresa1234")
    assert in_person(client, other_company, employee["id"], "enroll").status_code == 404  # no es suyo

    validator = validator_headers(client, company_headers)
    assert in_person(client, validator, employee["id"], "enroll").status_code == 403
    employee_headers = login(client, "juan@empresa.com", "Empleado123")
    assert in_person(client, employee_headers, employee["id"], "verify").status_code == 403

    client.patch(f"/api/employees/{employee['id']}/status", json={"active": False}, headers=company_headers)
    inactive = in_person(client, company_headers, employee["id"], "enroll")
    assert inactive.status_code == 409 and inactive.json()["code"] == "EMPLOYEE_INACTIVE"
