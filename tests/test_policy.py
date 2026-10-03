"""Política de verificación configurable por COMPANY y anti-spoofing / migración de modelo."""

from app.core.crypto import decrypt_bytes, encrypt_bytes
from app.core.database import SessionLocal
from app.facial_recognition.matcher import embedding_from_bytes, embedding_to_bytes
from app.models import FaceEmbedding
from tests.conftest import approved_employee, create_employee, login, submit_enrollment
from tests.test_face import _verify

URL = "/api/settings/verification"


def set_policy(client, company_headers, **changes):
    response = client.put(URL, json=changes, headers=company_headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_policy_defaults_and_permissions(client, company_headers):
    policy = client.get(URL, headers=company_headers).json()["data"]
    assert policy["block_glasses"] and policy["block_headwear"] and policy["block_mask"]
    assert policy["liveness_challenge"] and policy["anti_spoofing"] and policy["qr_enabled"]

    assert create_employee(client, company_headers).status_code == 201
    employee = login(client, "juan@empresa.com", "Empleado123")
    assert client.get(URL, headers=employee).status_code == 200  # el empleado la puede leer
    denied = client.put(URL, json={"block_mask": False}, headers=employee)
    assert denied.status_code == 403  # pero solo COMPANY la modifica

    updated = set_policy(client, company_headers, block_mask=False)
    assert updated["block_mask"] is False and updated["block_glasses"] is True
    assert updated["updated_by"] == "admin@empresa.com"


def test_company_can_allow_accessories(client, company_headers):
    headers = approved_employee(client, company_headers)
    masked = (b"mask:juan", b"mask:juan", b"mask:juan")
    assert _verify(client, headers, frontal=masked).json()["code"] == "ACCESSORIES_DETECTED"

    set_policy(client, company_headers, block_mask=False)
    assert _verify(client, headers, frontal=masked).json()["data"]["verified"] is True
    glasses = (b"glasses:juan",) * 3
    assert _verify(client, headers, frontal=glasses).json()["code"] == "ACCESSORIES_DETECTED"


def test_company_can_disable_liveness_and_qr(client, company_headers):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, liveness_challenge=False, qr_enabled=False)

    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert challenge["liveness_required"] is False
    files = [("images", ("f.jpg", b"face:juan", "image/jpeg"))]
    assert client.post("/api/verification/face", files=files, headers=headers).json()["data"]["verified"] is True

    qr = client.post("/api/users/me/qr", headers=headers)
    assert qr.status_code == 403 and qr.json()["code"] == "QR_DISABLED"


def test_antispoofing_blocks_verification_and_flags_enrollment(client, company_headers):
    headers = approved_employee(client, company_headers)
    spoof = _verify(client, headers, frontal=(b"spoof:juan", b"spoof:juan", b"face:juan"))
    assert spoof.status_code == 422 and spoof.json()["code"] == "SPOOF_DETECTED"
    # Un solo cuadro dudoso no bloquea (consenso).
    assert _verify(client, headers, frontal=(b"spoof:juan", b"face:juan", b"face:juan")).json()["data"]["verified"]

    set_policy(client, company_headers, anti_spoofing=False)
    assert _verify(client, headers, frontal=(b"spoof:juan",) * 3).json()["data"]["verified"] is True

    set_policy(client, company_headers, anti_spoofing=True)
    create_employee(client, company_headers, number="EMP-2", email="ana@empresa.com")
    ana = login(client, "ana@empresa.com", "Empleado123")
    submitted = submit_enrollment(client, ana, frontal=(b"spoof:ana", b"spoof:ana", b"spoof:ana"), turn_person="ana")
    assert submitted.status_code == 201  # el registro no se bloquea: lo decide el revisor
    detail = client.get(f"/api/enrollments/{submitted.json()['data']['enrollment_id']}", headers=company_headers)
    assert detail.json()["data"]["flagged_accessories"] == ["SPOOF"]


def test_model_change_migrates_from_reference_photo(client, company_headers):
    """Al cambiar el motor de reconocimiento, los aprobados no se registran de nuevo."""
    headers = approved_employee(client, company_headers)
    with SessionLocal() as db:
        rows = db.query(FaceEmbedding).all()
        for row in rows:  # simula embeddings generados por un modelo anterior
            row.model_name = "modelo-anterior"
            vector = embedding_from_bytes(decrypt_bytes(row.embedding_encrypted), row.dimension)
            row.embedding_encrypted = encrypt_bytes(embedding_to_bytes(vector))
        db.commit()

    assert _verify(client, headers).json()["data"]["verified"] is True
    with SessionLocal() as db:
        current = db.query(FaceEmbedding).filter(FaceEmbedding.model_name == "fake-model").count()
    # 1 desde la foto de referencia + 1 captura verificada para completar muestras.
    assert current == 2


def test_confidence_calibration_matches_operating_point():
    from app.facial_recognition.calibration import match_confidence, similarity_for_confidence

    # 90 % de confianza ≈ el umbral histórico (0.40); 99.999 % exige 0.653.
    assert abs(similarity_for_confidence(0.90, "fusion") - 0.408) < 0.002
    assert abs(similarity_for_confidence(0.99999, "fusion") - 0.653) < 0.002
    assert similarity_for_confidence(0.99, "fusion") > similarity_for_confidence(0.95, "fusion") > 0.40
    assert abs(match_confidence(similarity_for_confidence(0.97, "sface"), "sface") - 0.97) < 1e-9
    assert match_confidence(0.9, "fusion") > 0.999 and match_confidence(0.1, "fusion") < 0.001


def test_company_sets_required_confidence(client, company_headers):
    assert client.get(URL, headers=company_headers).json()["data"]["min_confidence"] == 0.99999
    assert set_policy(client, company_headers, min_confidence=0.99)["min_confidence"] == 0.99
    assert set_policy(client, company_headers, min_confidence=0.99999)["min_confidence"] == 0.99999
    assert client.put(URL, json={"min_confidence": 0.999999}, headers=company_headers).status_code == 422
    too_low = client.put(URL, json={"min_confidence": 0.5}, headers=company_headers)
    assert too_low.status_code == 422 and too_low.json()["errors"][0]["field"] == "min_confidence"


def test_verification_reports_calibrated_confidence(client, company_headers):
    headers = approved_employee(client, company_headers)
    result = _verify(client, headers).json()["data"]
    assert result["verified"] is True
    assert result["confidence"] >= 0.99999  # misma persona: supera el 99.999 % exigido


def test_confidence_range_starts_at_80_and_is_effective():
    from app.core.config import settings
    from app.facial_recognition.calibration import similarity_for_confidence

    # El piso técnico no anula los niveles bajos: 80 % exige su propia similitud (0.386).
    assert similarity_for_confidence(0.80, "fusion") >= settings.FACE_MATCH_THRESHOLD
    assert abs(similarity_for_confidence(0.80, "fusion") - 0.386) < 0.002
