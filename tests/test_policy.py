"""Política de verificación (la configura el ADMIN para cada empresa) y anti-spoofing / migración de modelo."""

from app.core.crypto import decrypt_bytes, encrypt_bytes
from app.core.database import SessionLocal
from app.facial_recognition.matcher import embedding_from_bytes, embedding_to_bytes
from app.models import Employee, FaceEmbedding
from tests.conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    FakePipeline,
    approved_employee,
    create_employee,
    login,
    submit_enrollment,
)
from tests.test_face import _verify

URL = "/api/settings/verification"


def admin_company(client, company_headers) -> tuple[str, dict[str, str]]:
    """(ruta de la empresa de esa sesión en la consola del ADMIN, sesión del ADMIN)."""
    company_id = client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]
    return f"/api/admin/companies/{company_id}", login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


def admin_policy(client, company_headers) -> tuple[str, dict[str, str]]:
    """(ruta, sesión del ADMIN) para configurar la política de la empresa de esa sesión."""
    base, admin = admin_company(client, company_headers)
    return f"{base}/verification-policy", admin


def set_policy(client, company_headers, **changes):
    """La política la configura el ADMIN de la plataforma (como desde su consola)."""
    url, admin = admin_policy(client, company_headers)
    response = client.put(url, json=changes, headers=admin)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_policy_defaults_and_permissions(client, company_headers):
    policy = client.get(URL, headers=company_headers).json()["data"]
    assert policy["block_glasses"] and policy["block_headwear"] and policy["block_mask"]
    assert policy["liveness_challenge"] and policy["anti_spoofing"] and policy["qr_enabled"]

    assert create_employee(client, company_headers).status_code == 201
    employee = login(client, "juan@empresa.com", "Empleado123")
    assert client.get(URL, headers=employee).status_code == 200  # el empleado la puede leer
    # Nadie de la empresa la modifica: no hay ruta para hacerlo, y la del ADMIN les responde 403.
    assert client.put(URL, json={"block_mask": False}, headers=company_headers).status_code == 405
    url, _admin = admin_policy(client, company_headers)
    assert client.put(url, json={"block_mask": False}, headers=company_headers).status_code == 403

    updated = set_policy(client, company_headers, block_mask=False)
    assert updated["block_mask"] is False and updated["block_glasses"] is True
    assert updated["updated_by"] == ADMIN_EMAIL  # en la consola de la plataforma...
    assert client.get(URL, headers=company_headers).json()["data"]["updated_by"] is None  # ...no fuera de ella


def test_the_admin_configures_only_existing_companies(client, admin_headers):
    missing = "/api/admin/companies/999/verification-policy"
    assert client.get(missing, headers=admin_headers).status_code == 404
    assert client.put(missing, json={"block_mask": False}, headers=admin_headers).status_code == 404


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
        current = db.query(FaceEmbedding).filter(FaceEmbedding.model_name == "fake-model").all()
    # 1 desde la foto de referencia (ancla, decidió la verificación). La captura es idéntica a ella:
    # no enseña nada nuevo, así que no se aprende (face_learning).
    assert [(row.learned, row.matches) for row in current] == [(False, 1)]


def _old_model_only(employee_id: int) -> None:
    with SessionLocal() as db:
        for row in db.query(FaceEmbedding).filter(FaceEmbedding.employee_id == employee_id):
            row.model_name = "modelo-anterior"
        db.commit()


def test_migration_without_a_photo_or_with_an_engine_crash_is_not_retried_on_every_capture(
    client, company_headers, monkeypatch
):
    """Un aprobado que no se puede migrar (sin foto o el motor truena) no se reintenta en cada captura
    ni tumba la verificación: queda bloqueado un rato y la falla del motor se registra."""
    from app.models import FaceEnrollment
    from app.services import face_service

    headers = approved_employee(client, company_headers)
    employee_id = client.get("/api/users/me", headers=headers).json()["data"]["employee"]["id"]
    _old_model_only(employee_id)
    with SessionLocal() as db:
        db.query(FaceEnrollment).update({"photo_encrypted": None})
        db.commit()
    assert _verify(client, headers).json()["code"] == "FACE_NOT_REGISTERED"
    assert face_service.migration_blocked(employee_id)

    face_service.clear_migration_blocks()
    with SessionLocal() as db:
        db.query(FaceEnrollment).update({"photo_encrypted": encrypt_bytes(b"face:juan")})
        db.commit()

    def crash(*_args, **_kwargs):
        raise RuntimeError("onnxruntime: memoria agotada")

    monkeypatch.setattr(FakePipeline, "analyze_frontal", crash, raising=True)
    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        pipeline = FakePipeline()
        assert face_service.FaceService(db, pipeline).references_for(employee) == []
    assert face_service.migration_blocked(employee_id)
    assert employee_id in face_service.blocked_migrations()


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
    url, admin = admin_policy(client, company_headers)
    assert client.put(url, json={"min_confidence": 0.999999}, headers=admin).status_code == 422
    assert client.get(url, headers=admin).json()["data"]["min_confidence"] == 0.99999
    too_low = client.put(url, json={"min_confidence": 0.5}, headers=admin)
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


def test_the_admin_sets_a_minimum_capture_quality(client, company_headers):
    """Una captura pobre (poca luz, desenfocada) no verifica si la política exige más calidad."""
    headers = approved_employee(client, company_headers)
    assert client.get(URL, headers=company_headers).json()["data"]["min_capture_quality"] == 0.4
    poor = (b"dim:juan", b"dim:juan", b"dim:juan")
    assert _verify(client, headers, frontal=poor).json()["data"]["verified"] is True  # 0.45 supera el 0.40 de fábrica
    set_policy(client, company_headers, min_capture_quality=0.5)
    rejected = _verify(client, headers, frontal=poor)
    assert rejected.status_code == 422 and rejected.json()["code"] == "LOW_QUALITY"
    assert "calidad suficiente" in rejected.json()["message"]
    url, admin = admin_policy(client, company_headers)
    assert client.put(url, json={"min_capture_quality": 0.95}, headers=admin).status_code == 422  # tope 0.9
