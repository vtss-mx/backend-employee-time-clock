"""Bordes del punto de control (CheckpointService y la galería 1:N de face_gallery): quién puede operarlo,
qué hace con una foto o pantalla, con capturas de personas distintas, con una galería vacía, con el dueño
de un QR que ya no tiene muestras útiles y con empleados cuyo rostro hay que migrar tras cambiar el motor.
"""

import pytest

from app.core.database import SessionLocal
from app.core.exceptions import PermissionDeniedError
from app.models import FaceEmbedding, FaceEnrollment, User, VerificationLog
from app.services.catalog_service import get_catalogs
from app.services.checkpoint_service import CheckpointService
from app.services.face_gallery import face_galleries
from tests.conftest import COMPANY_EMAIL, qr_content
from tests.test_capture_security import CHECKPOINT, attempt
from tests.test_policy import _old_model_only
from tests.test_validators import approved, identify_face, validator_headers


@pytest.fixture(autouse=True)
def _fresh_gallery():
    face_galleries.clear()  # cada prueba usa una BD nueva
    yield


def _last_attempt() -> tuple[int | None, str | None]:
    """(empleado, motivo) del último intento en la bitácora."""
    with SessionLocal() as db:
        log = db.query(VerificationLog).order_by(VerificationLog.id.desc()).first()
        assert log is not None
        return log.employee_id, log.reason


def _not_identified(response, reason: str) -> None:
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["verified"] is False and data["message"] == get_catalogs().reason_message(reason)


def test_only_a_validator_account_operates_a_checkpoint():
    with SessionLocal() as db:
        company_admin = db.query(User).filter_by(email=COMPANY_EMAIL).one()
        with pytest.raises(PermissionDeniedError) as error:
            CheckpointService(db, company_admin)
    assert error.value.code == "VALIDATOR_REQUIRED"


def test_a_face_only_validator_cannot_inspect_a_qr(client, company_headers):
    juan = approved(client, company_headers, "juan", number="EMP-001")
    headers = validator_headers(client, company_headers, mode="FACE")
    qr = qr_content(juan["id"])
    denied = client.post("/api/checkpoint/qr/inspect", json={"qr_content": qr}, headers=headers)
    assert denied.status_code == 409 and denied.json()["code"] == "VALIDATOR_METHOD_NOT_ALLOWED"


def test_an_empty_gallery_identifies_nobody(client, company_headers):
    headers = validator_headers(client, company_headers, mode="FACE")
    _not_identified(identify_face(client, headers, "juan"), "EMPTY_GALLERY")
    assert _last_attempt() == (None, "EMPTY_GALLERY")


def test_captures_of_different_people_are_not_concluded(client, company_headers):
    """Cada captura debe señalar a la MISMA persona: una toma con dos rostros distintos no se adivina."""
    approved(client, company_headers, "juan", number="EMP-001")
    approved(client, company_headers, "ana", number="EMP-002")
    headers = validator_headers(client, company_headers, mode="FACE")
    mixed = attempt(client, headers, CHECKPOINT, frontal=("face:juan", "face:ana", "face:juan"))
    _not_identified(mixed, "INCONSISTENT_MATCH")
    assert _last_attempt() == (None, "INCONSISTENT_MATCH")


def test_a_photo_or_screen_is_rejected_before_searching_the_gallery(client, company_headers):
    approved(client, company_headers, "juan", number="EMP-001")
    headers = validator_headers(client, company_headers, mode="FACE")
    spoofed = identify_face(client, headers, "juan", kind="spoof")
    assert spoofed.status_code == 422 and spoofed.json()["code"] == "SPOOF_DETECTED"
    assert _last_attempt() == (None, "SPOOF_DETECTED")


def test_qr_holder_without_usable_face_samples_is_not_confirmed(client, company_headers):
    """El dueño del QR ya no tiene muestras del motor actual ni foto de dónde generarlas: el rostro no se
    puede confirmar y queda en su bitácora como FACE_NOT_REGISTERED (no como un impostor)."""
    ana = approved(client, company_headers, "ana", number="EMP-001")
    _old_model_only(ana["id"])
    with SessionLocal() as db:
        db.query(FaceEnrollment).update({"photo_encrypted": None})
        db.commit()
    headers = validator_headers(client, company_headers, mode="QR_AND_FACE")
    qr = qr_content(ana["id"])
    assert client.post("/api/checkpoint/qr/inspect", json={"qr_content": qr}, headers=headers).status_code == 200

    _not_identified(identify_face(client, headers, "ana", qr=qr), "FACE_NOT_REGISTERED")
    assert _last_attempt() == (ana["id"], "FACE_NOT_REGISTERED")


def test_the_gallery_migrates_approved_faces_after_an_engine_change(client, company_headers):
    """Tras cambiar de motor, el validador genera las muestras del modelo actual desde la foto aprobada
    (por lotes) y así puede identificar a quien aún no se había verificado con el motor nuevo."""
    juan = approved(client, company_headers, "juan", number="EMP-001")
    ana = approved(client, company_headers, "ana", number="EMP-002")
    for employee in (juan, ana):
        _old_model_only(employee["id"])
    headers = validator_headers(client, company_headers, mode="FACE")

    found = identify_face(client, headers, "ana").json()["data"]
    assert found["verified"] is True and found["employee_id"] == ana["id"]
    with SessionLocal() as db:
        migrated = {row.employee_id for row in db.query(FaceEmbedding).filter_by(model_name="fake-model")}
    assert migrated == {juan["id"], ana["id"]}
