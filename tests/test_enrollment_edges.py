"""Bordes del registro facial (EnrollmentService) y de la verificación en persona (VerificationService):
quién puede enviar su rostro, cuántas capturas, el formato de la foto de referencia, una foto que ya no
se puede descifrar, la suplantación que solo se ve en el giro y el empleado inactivo frente a la empresa.
"""

import logging

import cv2
import numpy as np
import pytest

from app.core.database import SessionLocal
from app.core.exceptions import PermissionDeniedError, UnprocessableError
from app.models import FaceEnrollment, User
from app.services.enrollment_service import EnrollmentService, _image_type
from tests.conftest import (
    COMPANY_EMAIL,
    FakePipeline,
    approved_employee,
    create_employee,
    login,
    submit_enrollment,
    turn_files,
)
from tests.test_capture_security import employee_id
from tests.test_in_person_face import in_person


def _submit(email: str, images: list[bytes]):
    """Envía el registro directamente al servicio (sin la ruta, que ya limita las capturas)."""
    with SessionLocal() as db:
        user = db.query(User).filter_by(email=email).one()
        service = EnrollmentService(db, user.current_company.id)
        return service.submit(user, images, FakePipeline(), challenge_id=None, challenge_images=())


def test_the_reference_photo_keeps_its_real_format():
    """El revisor ve la foto con su tipo real (PNG, WEBP o JPEG) aunque el archivo diga otra cosa."""
    image = np.full((8, 8, 3), 128, dtype=np.uint8)
    for extension, content_type in ((".png", "image/png"), (".webp", "image/webp"), (".jpg", "image/jpeg")):
        ok, encoded = cv2.imencode(extension, image)
        assert ok and _image_type(encoded.tobytes()) == content_type


def test_only_active_employees_of_the_company_enroll(client, company_headers):
    with pytest.raises(PermissionDeniedError):
        _submit(COMPANY_EMAIL, [b"face:juan"])  # la cuenta de la empresa no es un empleado

    assert create_employee(client, company_headers).status_code == 201
    with SessionLocal() as db:
        employee = db.query(User).filter_by(email="juan@empresa.com").one().employee
        employee.active = False  # dado de baja mientras su sesión seguía abierta
        db.commit()
    with pytest.raises(PermissionDeniedError):
        _submit("juan@empresa.com", [b"face:juan"])


def test_an_enrollment_takes_one_to_five_frontal_captures(client, company_headers):
    assert create_employee(client, company_headers).status_code == 201
    for images in ([], [b"face:juan"] * 6):
        with pytest.raises(UnprocessableError) as error:
            _submit("juan@empresa.com", images)
        assert error.value.code == "INVALID_FRAME_COUNT"


def test_an_approved_face_is_not_enrolled_again(client, company_headers):
    headers = approved_employee(client, company_headers)
    again = submit_enrollment(client, headers)
    assert again.status_code == 409 and again.json()["code"] == "ENROLLMENT_APPROVED"


def test_an_unreadable_photo_still_shows_the_enrollment_to_the_reviewer(client, company_headers, caplog):
    assert create_employee(client, company_headers).status_code == 201
    headers = login(client, "juan@empresa.com", "Empleado123")
    enrollment = submit_enrollment(client, headers).json()["data"]["enrollment_id"]
    with SessionLocal() as db:
        db.get(FaceEnrollment, enrollment).photo_encrypted = b"cifrada-con-otra-llave"
        db.commit()

    with caplog.at_level(logging.ERROR, logger="app.services.enrollment_service"):
        detail = client.get(f"/api/enrollments/{enrollment}", headers=company_headers)
    assert detail.status_code == 200 and detail.json()["data"]["photo"] is None
    assert detail.json()["data"]["employee_id"] == employee_id(client, headers)
    assert f"La foto del registro facial {enrollment} es ilegible" in caplog.text


def test_a_screen_seen_only_in_the_turn_is_flagged_for_the_reviewer(client, company_headers):
    """Una sola frontal sospechosa no basta para marcarla (consenso), pero con el giro también sospechoso
    son dos indicios: el autoregistro se envía marcado SPOOF para que la empresa lo revise."""
    assert create_employee(client, company_headers).status_code == 201
    headers = login(client, "juan@empresa.com", "Empleado123")
    frontal = (b"face:juan", b"spoof:juan", b"face:juan")
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", f, "image/jpeg")) for i, f in enumerate(frontal)]
    files += turn_files(challenge, "juan", image="spoof-turn:{person}")
    submitted = client.post(
        "/api/enrollment/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
    )
    assert submitted.status_code == 201, submitted.text
    detail = client.get(f"/api/enrollments/{submitted.json()['data']['enrollment_id']}", headers=company_headers)
    assert detail.json()["data"]["flagged_accessories"] == ["SPOOF"]


def test_the_company_does_not_verify_an_inactive_employee_in_person(client, company_headers):
    employee = create_employee(client, company_headers).json()["data"]
    assert in_person(client, company_headers, employee["id"], "enroll").status_code == 201
    client.patch(f"/api/employees/{employee['id']}/status", json={"active": False}, headers=company_headers)
    inactive = in_person(client, company_headers, employee["id"], "verify")
    assert inactive.status_code == 409 and inactive.json()["code"] == "EMPLOYEE_INACTIVE"
