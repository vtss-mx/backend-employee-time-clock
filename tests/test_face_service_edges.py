"""Bordes del análisis facial común (face_service, identity_core, face_capture_service): capturas que
OpenCV no puede leer, fallas inesperadas del motor, muestras ilegibles, la migración desde la foto de
referencia cuando no se puede hacer y la revisión previa según quién opera la cámara.

Regla de fondo (AGENTS §5): ninguna falla del motor o de un dato cifrado tumba la petición ni se
responde con un 500 opaco; cada una tiene su código y queda registrada.
"""

import logging

import cv2
import numpy as np
import pytest

from app.core.crypto import encrypt_bytes
from app.core.database import SessionLocal
from app.core.exceptions import UnprocessableError
from app.dependencies import get_pipeline
from app.facial_recognition.matcher import embedding_to_bytes
from app.facial_recognition.pipeline import DEFAULT_POLICY
from app.main import app
from app.models import Employee, FaceEmbedding, FaceEnrollment, VerificationLog
from app.services import face_service
from app.services.catalog_service import get_catalogs
from app.services.face_service import FaceService, analyze_frames
from app.services.identity_core import MAX_FRONTAL_FRAMES, ensure_frame_count
from tests.conftest import FakePipeline, approved_employee, create_employee, login
from tests.test_capture_security import attempt, employee_id
from tests.test_face import _verify
from tests.test_policy import _old_model_only
from tests.test_validators import validator_headers

CHECK = "/api/face/check"


class FailingPipeline(FakePipeline):
    """FakePipeline con fallas del motor:
    b"unreadable:<persona>"    captura frontal que OpenCV no puede leer (cv2.error)
    b"crash:<persona>"         el motor truena con la captura frontal (ONNX, memoria...)
    giro b"crash-turn-...:..." el motor truena con la captura del giro
    """

    def analyze_frontal(self, image_bytes, *, policy=DEFAULT_POLICY, enforce_accessories=True):
        if image_bytes.startswith(b"unreadable:"):
            raise cv2.error("captura ilegible")
        if image_bytes.startswith(b"crash:"):
            raise RuntimeError("onnxruntime: memoria agotada")
        return super().analyze_frontal(image_bytes, policy=policy, enforce_accessories=enforce_accessories)

    def analyze_turn(self, image_bytes, direction, *, policy=DEFAULT_POLICY):
        if image_bytes.startswith(b"crash-"):
            raise RuntimeError("onnxruntime: memoria agotada")
        return super().analyze_turn(image_bytes, direction, policy=policy)


@pytest.fixture
def failing_engine(client):
    app.dependency_overrides[get_pipeline] = FailingPipeline
    return client


def _check(client, headers, *images: bytes, **data):
    files = [("images", (f"c{i}.jpg", image, "image/jpeg")) for i, image in enumerate(images)]
    return client.post(CHECK, files=files, data=data, headers=headers)


def _employee_headers(client, company_headers) -> dict[str, str]:
    assert create_employee(client, company_headers).status_code == 201
    return login(client, "juan@empresa.com", "Empleado123")


# ---------------------------------------------------------------- fallas del motor


def test_an_unreadable_capture_is_a_422_that_names_the_photo(failing_engine, company_headers):
    headers = _employee_headers(failing_engine, company_headers)
    response = _check(failing_engine, headers, b"face:juan", b"unreadable:juan")
    assert response.status_code == 422 and response.json()["code"] == "INVALID_IMAGE"
    assert response.json()["message"] == "Foto 2: " + get_catalogs().face_error_message("INVALID_IMAGE")


def test_an_engine_crash_is_a_503_and_is_logged(failing_engine, company_headers, caplog):
    headers = _employee_headers(failing_engine, company_headers)
    with caplog.at_level(logging.ERROR, logger="app.services.face_service"):
        response = _check(failing_engine, headers, b"crash:juan")
    assert response.status_code == 503 and response.json()["code"] == "FACE_PROCESSING_ERROR"
    assert response.json()["message"] == get_catalogs().face_error_message("FACE_PROCESSING_ERROR")
    crash = next(r for r in caplog.records if r.getMessage() == "Error en el motor de reconocimiento facial")
    assert crash.exc_info is not None  # con su stack trace, para el seguimiento del ADMIN


def test_an_engine_crash_on_the_turn_is_a_503_not_a_failed_attempt(failing_engine, company_headers):
    """Una falla del motor no es culpa del empleado: no cuenta como intento fallido ni para el bloqueo."""
    headers = approved_employee(failing_engine, company_headers)
    response = attempt(failing_engine, headers, turn="crash-turn:juan")
    assert response.status_code == 503 and response.json()["code"] == "FACE_PROCESSING_ERROR"
    with SessionLocal() as db:
        assert db.query(VerificationLog).filter_by(employee_id=employee_id(failing_engine, headers)).count() == 0


def test_an_engine_that_rejects_accessories_by_itself_answers_like_the_consensus():
    """Si el motor rechaza los accesorios por su cuenta (aunque se le pida solo reportarlos), la respuesta
    es la misma del consenso: 422 ACCESSORIES_DETECTED con los nombres del catálogo y qué foto fue."""

    class StrictPipeline(FakePipeline):
        def analyze_frontal(self, image_bytes, *, policy=DEFAULT_POLICY, enforce_accessories=True):
            return super().analyze_frontal(image_bytes, policy=policy, enforce_accessories=True)

    with pytest.raises(UnprocessableError) as error:
        analyze_frames(StrictPipeline(), [b"face:juan", b"glasses:juan"], policy=DEFAULT_POLICY)
    assert error.value.code == "ACCESSORIES_DETECTED" and error.value.details == {"accessories": ["GLASSES"]}
    assert error.value.message == "Foto 2: " + get_catalogs().accessories_message(["GLASSES"])


# ---------------------------------------------------------------- cantidad de capturas


def test_every_identification_takes_one_to_three_frontal_frames():
    for images in ([], [b"face:juan"] * (MAX_FRONTAL_FRAMES + 1)):
        with pytest.raises(UnprocessableError) as error:
            ensure_frame_count(images)
        assert error.value.code == "INVALID_FRAME_COUNT"
    ensure_frame_count([b"face:juan"])
    ensure_frame_count([b"face:juan"] * MAX_FRONTAL_FRAMES)


def test_an_enrollment_analysis_takes_one_to_five_samples():
    with SessionLocal() as db:
        faces = FaceService(db, FakePipeline())
        for images in ([], [b"face:juan"] * 6):
            with pytest.raises(UnprocessableError) as error:
                faces.analyze_enrollment(images, policy=DEFAULT_POLICY)
            assert error.value.code == "INVALID_SAMPLE_COUNT"


# ---------------------------------------------------------------- muestras y migración


def test_a_sample_with_the_wrong_size_is_skipped_and_logged(client, company_headers, caplog):
    """Una muestra dañada (otra dimensión) se omite: el empleado se verifica con las demás."""
    headers = approved_employee(client, company_headers)
    with SessionLocal() as db:
        sample = db.query(FaceEmbedding).filter_by(employee_id=employee_id(client, headers)).first()
        sample.embedding_encrypted = encrypt_bytes(embedding_to_bytes(np.ones(64)))
        damaged = sample.id
        db.commit()
    with caplog.at_level(logging.ERROR, logger="app.services.face_service"):
        assert _verify(client, headers).json()["data"]["verified"] is True
    assert f"Muestra facial {damaged} ilegible" in caplog.text


def _photo(employee: int, photo: bytes) -> None:
    with SessionLocal() as db:
        db.query(FaceEnrollment).filter_by(employee_id=employee).update({"photo_encrypted": photo})
        db.commit()


def test_an_unreadable_reference_photo_is_not_migrated_nor_retried(client, company_headers, caplog, monkeypatch):
    """Cambió el motor y la foto aprobada no se puede descifrar (otra llave): se registra, el empleado
    queda sin rostro con qué compararse y no se vuelve a intentar en cada captura."""
    headers = approved_employee(client, company_headers)
    employee = employee_id(client, headers)
    _old_model_only(employee)
    _photo(employee, b"cifrado-con-otra-llave")

    with caplog.at_level(logging.ERROR, logger="app.services.face_service"):
        assert _verify(client, headers).json()["code"] == "FACE_NOT_REGISTERED"
    assert f"La foto aprobada del empleado {employee} es ilegible" in caplog.text
    assert face_service.migration_blocked(employee)

    decrypted: list[bytes] = []
    monkeypatch.setattr(face_service, "try_decrypt", lambda data: decrypted.append(data))
    assert _verify(client, headers).json()["code"] == "FACE_NOT_REGISTERED"
    assert decrypted == []  # bloqueado: ni siquiera vuelve a leer la foto


def test_a_reference_photo_the_engine_rejects_blocks_its_migration(client, company_headers):
    headers = approved_employee(client, company_headers)
    employee = employee_id(client, headers)
    _old_model_only(employee)
    _photo(employee, encrypt_bytes(b"noface"))
    with SessionLocal() as db:
        assert FaceService(db, FakePipeline()).references_for(db.get(Employee, employee)) == []
    assert employee in face_service.blocked_migrations()


def test_migration_blocks_are_bounded_and_expire(monkeypatch):
    """La memoria de bloqueos tiene tope (se reinicia al llenarse) y cada bloqueo vence solo."""
    monkeypatch.setattr(face_service, "_MIGRATION_TRACKED_MAX", 2)
    for employee in (1, 2, 3):
        face_service.block_migration(employee)
    assert face_service.blocked_migrations() == {3}

    monkeypatch.setattr(face_service, "_MIGRATION_RETRY_SECONDS", 0.0)
    face_service.block_migration(4)
    assert face_service.migration_blocked(4) is False  # vencido: se reintenta y se olvida
    assert face_service.blocked_migrations() == {3}


# ---------------------------------------------------------------- revisión previa por rol


def test_precheck_leaves_headwear_to_whoever_may_decide_it(client, company_headers):
    """La prenda de cabeza: el validador la decide al saber quién es, la empresa puede omitirla y el
    empleado depende de su excepción registrada (no de lo que envíe)."""
    hat = b"hat:juan"
    employee = _employee_headers(client, company_headers)
    validator = validator_headers(client, company_headers, mode="FACE")

    assert _check(client, validator, hat).status_code == 200
    assert _check(client, company_headers, hat, allow_headwear="true").status_code == 200
    assert _check(client, company_headers, hat).json()["code"] == "ACCESSORIES_DETECTED"
    assert _check(client, employee, hat, allow_headwear="true").json()["code"] == "ACCESSORIES_DETECTED"
