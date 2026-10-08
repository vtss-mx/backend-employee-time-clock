"""Bordes del antifraude que los flujos completos no recorren: nombres del contrato de un caso, el motivo de respaldo,
el tipo de imagen de la evidencia, listas vacías en la lista de bloqueo, depuraciones que llegan a su tope de lotes,
una señal apagada del catálogo o de la empresa y una identificación con QR + rostro que no enseña a la galería."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import CatalogRiskSignal, FaceEmbedding, FraudCase, FraudEvidence, RiskAssessment
from app.repositories.risk_repository import AttackSignatureRepository
from app.services import fraud_case_service
from app.services.catalog_service import clear_catalog_cache
from app.services.face_signals import AttemptSignals
from app.services.fraud_case_service import reason_name
from app.services.fraud_cases import _kind_and_reason
from app.services.image_storage import image_type
from app.services.policy_service import PolicySnapshot
from app.services.risk_engine import config_for
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, approved_employee, login, qr_content
from tests.test_fraud_cases import CASES, cases, open_case
from tests.test_policy import set_policy
from tests.test_risk_engine import assessments, signals, verify
from tests.test_validators import approved, identify_face, validator_headers


def test_a_case_and_its_signals_travel_with_their_names(client, company_headers, admin_headers):
    _, case_id = open_case(client, company_headers)
    detail = client.get(f"{CASES}/{case_id}", headers=admin_headers).json()["data"]
    assert (detail["reason"], detail["reason_name"]) == ("SPOOF_DETECTED", "Posible foto o pantalla")
    listed = client.get(CASES, headers=admin_headers).json()["data"]["items"][0]
    assert listed["reason_name"] == "Posible foto o pantalla"


def test_the_signals_of_a_risk_case_travel_with_their_names(client, company_headers):
    headers = approved_employee(client, company_headers)
    signals(client, company_headers, CAMERA_LABEL_MISSING={"mode": "ENFORCE", "points": 60})
    admin = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)  # set_policy abrió otra sesión del ADMIN
    assert verify(client, headers, frontal=b"lowreal:juan", camera=None).json()["code"] == "RISK_DENIED"
    detail = client.get(f"{CASES}/{cases()[0].id}", headers=admin).json()["data"]
    named = {s["code"]: s["name"] for a in detail["attempts_detail"] for s in a["signals"]}
    assert named["CAMERA_LABEL_MISSING"] == "Cámara sin nombre" and detail["reason_name"] == "Cámara sin nombre"


def test_reason_names_fall_back_to_the_signal_or_the_code():
    assert reason_name("CAMERA_LABEL_MISSING") == "Cámara sin nombre"  # una señal del motor
    assert reason_name("RISK_ALERT") == "RISK_ALERT"  # sin catálogo: el código tal cual


def test_a_case_without_a_lock_reason_nor_signals_is_a_generic_alert():
    assert _kind_and_reason(None, AttemptSignals()) == ("OTHER", "RISK_ALERT")
    assert _kind_and_reason("NO_MATCH", AttemptSignals()) == ("OTHER", "NO_MATCH")


def test_evidence_keeps_the_image_type_of_its_bytes():
    assert image_type(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert image_type(b"\xff\xd8\xff\xe0") == "image/jpeg"


def test_blocking_or_counting_no_signatures_touches_nothing():
    with SessionLocal() as db:
        repo = AttackSignatureRepository(db)
        repo.upsert("PHASH", [], 1, 1, datetime.now(UTC), allowed=False)
        assert repo.companies_blocking("PHASH", []) == {}
        assert repo.release_platform("PHASH", [], datetime.now(UTC)) == 0


def test_each_purge_stops_at_its_batch_cap_and_continues_next_round(
    client, company_headers, admin_headers, monkeypatch
):
    _, case_id = open_case(client, company_headers)
    client.post(f"{CASES}/{case_id}/decision", json={"status": "INCONCLUSIVE"}, headers=admin_headers)
    monkeypatch.setattr(settings, "MAINTENANCE_MAX_BATCHES_PER_TABLE", 1)
    much_later = datetime.now(UTC) + timedelta(days=settings.FRAUD_CASE_RETENTION_DAYS + 1)
    with SessionLocal() as db:
        removed = fraud_case_service.purge(db, much_later, 2)
        # Un lote por tabla en esta vuelta: 2 de las 6 fotos y el caso (su evidencia sale con él).
        assert removed == {"evidencia de casos de fraude": 2, "casos de fraude decididos": 1}
        assert db.scalar(select(func.count()).select_from(FraudEvidence)) == 0
    assert cases() == []


def test_an_inactive_catalog_signal_is_not_evaluated():
    with SessionLocal() as db:
        db.execute(update(CatalogRiskSignal).where(CatalogRiskSignal.code == "FLASH_WEAK").values(active=False))
        db.commit()
    clear_catalog_cache()
    config = config_for(PolicySnapshot())
    assert "FLASH_WEAK" not in config.signals and "FLASH_FLAT" in config.signals


def test_replay_signals_turned_off_are_not_looked_for(client, company_headers):
    headers = approved_employee(client, company_headers)
    signals(client, company_headers, REPLAY_PERCEPTUAL={"mode": "OFF"}, KNOWN_ATTACK={"mode": "OFF"})
    assert verify(client, headers).json()["data"]["verified"] is True
    assert verify(client, headers).json()["data"]["verified"] is True  # la misma cara: sin reenvío perceptual
    assert {"REPLAY_PERCEPTUAL", "KNOWN_ATTACK"}.isdisjoint(r["code"] for r in assessments()[-1].reasons)


def test_qr_and_face_with_risk_is_allowed_but_does_not_teach_the_gallery(client, company_headers):
    ana = approved(client, company_headers, "ana", number="EMP-002")
    set_policy(client, company_headers, risk_medium_action="ALLOW")
    signals(client, company_headers, CAMERA_LABEL_MISSING={"mode": "ENFORCE", "points": 30})
    headers = validator_headers(client, company_headers, mode="QR_AND_FACE")
    qr = qr_content(ana["id"])
    client.post("/api/checkpoint/qr/inspect", json={"qr_content": qr}, headers=headers)
    result = identify_face(client, headers, "ana", qr=qr).json()["data"]
    assert result["verified"] is True and result["method"] == "QR_FACE"
    assert (assessments()[-1].tier, assessments()[-1].action) == ("MEDIUM", "ALLOW")
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(FaceEmbedding).where(FaceEmbedding.learned)) == 0
        assert db.scalar(select(func.count()).select_from(FraudCase)) == 0
        assert db.scalar(select(func.count()).select_from(RiskAssessment)) >= 1
