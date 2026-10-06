"""Casos de fraude (fase 1 del antifraude): se abren solos desde los intentos sospechosos o de riesgo alto, guardan
su evidencia cifrada en el bucket (decisión D1), los revisa el ADMIN y su decisión alimenta la lista de bloqueo,
la calibración (solo endurece) y la línea base de cada señal."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.object_storage import get_storage
from app.models import (
    AttackSignature,
    FaceAttemptMetric,
    FaceEmbedding,
    FraudCase,
    FraudEvidence,
    RiskAssessment,
    RiskSignalStat,
    StorageDeletion,
)
from app.services import fraud_case_service, image_storage, maintenance_service
from app.services.image_storage import FRAUD_EVIDENCE
from tests.conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    approved_employee,
    create_company,
    create_employee,
    login,
    turn_files,
)
from tests.storage_support import FakeStorage, swap_object
from tests.test_policy import set_policy
from tests.test_risk_engine import verify
from tests.test_validators import identify_face, validator_headers

CASES = "/api/admin/fraud-cases"


def spoof(client, headers, frontal=b"spoof:juan"):
    """Un intento con foto o pantalla frente a la cámara (rechazo de un candado: SPOOF_DETECTED)."""
    return verify(client, headers, frontal=frontal)


def cases() -> list[FraudCase]:
    with SessionLocal() as db:
        return list(db.scalars(select(FraudCase).order_by(FraudCase.id)))


def open_case(client, company_headers) -> tuple[dict, int]:
    """(sesión del empleado, caso abierto por dos intentos suyos con foto)."""
    headers = approved_employee(client, company_headers)
    assert spoof(client, headers).json()["code"] == "SPOOF_DETECTED"
    assert spoof(client, headers, frontal=b"spoof:juan@foto").json()["code"] == "SPOOF_DETECTED"
    return headers, cases()[0].id


# ---------------------------------------------------------------- se abren solos


def test_suspicious_attempts_of_the_same_person_add_up_to_one_case_with_its_evidence(
    client, company_headers, admin_headers
):
    _, case_id = open_case(client, company_headers)
    [case] = cases()
    assert (case.status, case.kind, case.reason, case.attempts, case.subject) == (
        "OPEN",
        "PRESENTATION",
        "SPOOF_DETECTED",
        2,
        f"employee:{case.employee_id}",
    )
    # Evidencia (D1): las capturas del intento, CIFRADAS en el bucket (nunca en la BD).
    assert case.evidence == 2 * settings.FRAUD_EVIDENCE_FRAMES_PER_ATTEMPT
    storage = get_storage()
    assert isinstance(storage, FakeStorage)
    objects = [name for name in storage.objects if "/fraud-cases/" in name]
    assert len(objects) == case.evidence and all(b"spoof" not in storage.objects[n][0] for n in objects)

    detail = client.get(f"{CASES}/{case_id}", headers=admin_headers).json()["data"]
    assert detail["employee"]["full_name"] and detail["company_name"] and detail["actor"] == "juan@empresa.com"
    assert [e["kind"] for e in detail["events"]] == ["OPENED"] and detail["events"][0][
        "note"
    ] == "Posible foto o pantalla"  # el motivo, por su nombre
    attempt = detail["attempts_detail"][0]
    assert attempt["reason"] == "SPOOF_DETECTED" and attempt["success"] is False and attempt["signatures"] == 2
    assert attempt["metrics"]["frontal_real_min"] == 0.01 and attempt["camera"] == "Cámara FaceTime HD"
    evidence = detail["evidence_items"]
    assert [(e["kind"], e["position"]) for e in evidence[:2]] == [("FRONTAL", 0), ("FRONTAL", 1)]

    image = client.get(f"{CASES}/{case_id}/evidence/{evidence[0]['id']}", headers=admin_headers)
    assert image.status_code == 200 and image.json()["code"] == "FRAUD_EVIDENCE"
    import base64

    assert base64.b64decode(image.json()["data"]["data"]) == b"spoof:juan"
    viewed = client.get(f"{CASES}/{case_id}", headers=admin_headers).json()["data"]["events"][0]
    assert viewed["kind"] == "EVIDENCE_VIEWED" and viewed["actor"] and viewed["note"] == "FRONTAL 1"


def test_a_validator_attempt_without_an_identified_person_is_the_operators_case(client, company_headers):
    approved_employee(client, company_headers)
    validator = validator_headers(client, company_headers, mode="FACE")
    assert identify_face(client, validator, "juan", kind="spoof").json()["code"] == "SPOOF_DETECTED"
    [case] = cases()
    assert case.subject.startswith("actor:") and case.employee_id is None


def test_only_the_first_attempts_keep_their_detail_and_evidence_is_capped(client, company_headers, monkeypatch):
    monkeypatch.setattr(settings, "FRAUD_CASE_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "FRAUD_EVIDENCE_MAX_PER_CASE", 4)
    open_case(client, company_headers)
    [case] = cases()
    assert case.attempts == 2 and case.evidence == 4
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(FraudEvidence)) == 4


def test_without_evidence_or_bucket_the_case_still_opens(client, company_headers, monkeypatch, caplog):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, fraud_evidence=False)
    spoof(client, headers)
    assert cases()[0].evidence == 0
    set_policy(client, company_headers, fraud_evidence=True)
    monkeypatch.setattr(get_storage(), "configured", False)
    spoof(client, headers)
    assert cases()[0].evidence == 0
    monkeypatch.setattr(get_storage(), "configured", True)
    get_storage().down.add("put")  # el bucket no responde: el caso sigue (falla registrada), nada a medias
    with caplog.at_level(logging.ERROR):
        assert spoof(client, headers).json()["code"] == "SPOOF_DETECTED"
    assert "No se pudo guardar la evidencia" in caplog.text and cases()[0].attempts == 3 and cases()[0].evidence == 0


# ---------------------------------------------------------------- bandeja del ADMIN


def test_the_admin_inbox_filters_and_counts(client, company_headers, admin_headers):
    open_case(client, company_headers)
    assert client.get(f"{CASES}/count", headers=admin_headers).json()["data"] == {"active": 1}
    listed = client.get(CASES, headers=admin_headers).json()["data"]
    assert listed["total"] == 1 and listed["items"][0]["status"] == "OPEN"
    company_id = listed["items"][0]["company_id"]
    for query, total in (
        ("?status=ALL", 1),
        ("?status=CONFIRMED", 0),
        (f"?company_id={company_id}", 1),
        ("?kind=REPLAY", 0),
        ("?kind=PRESENTATION", 1),
    ):
        assert client.get(f"{CASES}{query}", headers=admin_headers).json()["data"]["total"] == total, query
    invalid = client.get(f"{CASES}?status=CERRADO", headers=admin_headers)
    assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_CASE_STATUS"
    missing = client.get(f"{CASES}/999", headers=admin_headers)
    assert missing.status_code == 404 and missing.json()["code"] == "FRAUD_CASE_NOT_FOUND"
    assert client.get(CASES, headers=company_headers).status_code == 403


# ---------------------------------------------------------------- revisión y aprendizaje


def test_confirming_blocks_its_signatures_labels_its_attempts_and_forgets_learning(
    client, company_headers, admin_headers
):
    headers, case_id = open_case(client, company_headers)
    # Lo que el reconocimiento aprendió desde el primer intento sospechoso se olvidará.
    assert verify(client, headers).json()["data"]["verified"] is True
    with SessionLocal() as db:
        employee_id = cases()[0].employee_id
        db.add(
            FaceEmbedding(
                employee_id=employee_id,
                company_id=cases()[0].company_id,
                embedding_encrypted=b"x",
                model_name="fake-model",
                dimension=128,
                detection_score=0.9,
                quality_score=0.9,
                active=True,
                learned=True,
                created_at=datetime.now(UTC) + timedelta(seconds=1),  # aprendida después del primer intento
            )
        )
        db.commit()
    url = f"{CASES}/{case_id}/decision"
    taken = client.post(url, json={"status": "IN_REVIEW"}, headers=admin_headers)
    assert taken.status_code == 200 and taken.json()["data"]["status"] == "IN_REVIEW"
    assert taken.json()["data"]["decided_by"] is None
    same = client.post(url, json={"status": "IN_REVIEW"}, headers=admin_headers)
    assert same.status_code == 409 and same.json()["code"] == "FRAUD_CASE_SAME_STATUS"
    no_note = client.post(url, json={"status": "CONFIRMED", "note": "  "}, headers=admin_headers)
    assert no_note.status_code == 422 and no_note.json()["code"] == "FRAUD_NOTE_REQUIRED"
    invalid = client.post(url, json={"status": "OPEN", "note": "x"}, headers=admin_headers)
    assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_CASE_STATUS"

    confirmed = client.post(url, json={"status": "CONFIRMED", "note": "Foto del gafete"}, headers=admin_headers)
    assert confirmed.status_code == 200 and confirmed.json()["code"] == "FRAUD_CASE_DECIDED"
    data = confirmed.json()["data"]
    assert data["status"] == "CONFIRMED" and data["decision_note"] == "Foto del gafete" and data["decided_by"]
    kinds = [e["kind"] for e in data["events"]]
    assert {"STATUS_CHANGED", "SIGNATURES_BLOCKED", "LEARNING_FORGOTTEN"} <= set(kinds)
    with SessionLocal() as db:
        signatures = db.scalars(select(AttackSignature)).all()
        labels = db.scalars(select(FaceAttemptMetric.fraud_label).where(FaceAttemptMetric.success.is_(False))).all()
        learned = db.scalar(select(func.count()).select_from(FaceEmbedding).where(FaceEmbedding.learned.is_(True)))
    # Las huellas de sus capturas: dos distintas del primer intento y una del segundo (dos fotos iguales).
    assert len(signatures) == 3 and all(s.company_id is not None and not s.allowed for s in signatures)
    assert labels == ["FRAUD", "FRAUD"] and learned == 0
    assert client.get(f"{CASES}/count", headers=admin_headers).json()["data"] == {"active": 0}

    # Era un falso positivo: las huellas se liberan y la etiqueta cambia (la línea base también).
    released = client.post(url, json={"status": "FALSE_POSITIVE", "note": "Era su credencial"}, headers=admin_headers)
    assert "SIGNATURES_RELEASED" in [e["kind"] for e in released.json()["data"]["events"]]
    with SessionLocal() as db:
        assert all(s.allowed for s in db.scalars(select(AttackSignature)))
        assert set(db.scalars(select(FaceAttemptMetric.fraud_label).where(FaceAttemptMetric.success.is_(False)))) == {
            "GENUINE"
        }
    inconclusive = client.post(url, json={"status": "INCONCLUSIVE"}, headers=admin_headers).json()["data"]
    assert inconclusive["status"] == "INCONCLUSIVE"


def test_confirmed_signals_feed_the_company_baseline_and_risk_labels(client, company_headers):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, risk_signals={"CAMERA_LABEL_MISSING": {"mode": "ENFORCE", "points": 60}})
    admin_headers = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)  # set_policy abrió otra sesión del ADMIN
    assert verify(client, headers, frontal=b"lowreal:juan", camera=None).json()["code"] == "RISK_DENIED"
    case_id = cases()[0].id
    url = f"{CASES}/{case_id}/decision"
    client.post(url, json={"status": "CONFIRMED", "note": "Script"}, headers=admin_headers)
    with SessionLocal() as db:
        stats = {s.signal: (s.confirmed, s.false_positive) for s in db.scalars(select(RiskSignalStat))}
        assert db.scalar(select(RiskAssessment.fraud_label)) == "FRAUD"
    assert stats["CAMERA_LABEL_MISSING"] == (1, 0)
    client.post(url, json={"status": "FALSE_POSITIVE", "note": "Navegador viejo"}, headers=admin_headers)
    with SessionLocal() as db:
        stats = {s.signal: (s.confirmed, s.false_positive) for s in db.scalars(select(RiskSignalStat))}
    assert stats["CAMERA_LABEL_MISSING"] == (0, 1)
    # La línea base se ve en la política del ADMIN.
    company_id = cases()[0].company_id
    policy = client.get(f"/api/admin/companies/{company_id}/verification-policy", headers=admin_headers).json()["data"]
    camera = next(s for s in policy["risk_signals"] if s["code"] == "CAMERA_LABEL_MISSING")
    assert (camera["confirmed"], camera["false_positive"]) == (0, 1)


def test_a_signature_confirmed_in_two_companies_becomes_platform_wide(client, company_headers, admin_headers):
    first = approved_employee(client, company_headers)
    assert spoof(client, first, frontal=b"spoof:juan@kit").json()["code"] == "SPOOF_DETECTED"
    assert create_company(client, admin_headers).status_code == 201
    other_company = login(client, "admin@panificadora.com", "Empresa1234")
    other = approved_employee(client, other_company, email="luis@empresa.com", number="B-1")
    assert spoof(client, other, frontal=b"spoof:juan@kit").json()["code"] == "SPOOF_DETECTED"
    for case in cases():
        decided = client.post(
            f"{CASES}/{case.id}/decision", json={"status": "CONFIRMED", "note": "Mismo kit"}, headers=admin_headers
        )
        assert decided.status_code == 200
    with SessionLocal() as db:
        platform = db.scalars(select(AttackSignature).where(AttackSignature.company_id.is_(None))).all()
    assert len(platform) == 1 and platform[0].companies == 2 and not platform[0].allowed
    # Si en una deja de estar confirmado, ya no vale para toda la plataforma.
    client.post(
        f"{CASES}/{cases()[0].id}/decision", json={"status": "FALSE_POSITIVE", "note": "Error"}, headers=admin_headers
    )
    with SessionLocal() as db:
        assert all(s.allowed for s in db.scalars(select(AttackSignature).where(AttackSignature.company_id.is_(None))))


def test_notes_go_to_the_history(client, company_headers, admin_headers):
    _, case_id = open_case(client, company_headers)
    noted = client.post(f"{CASES}/{case_id}/notes", json={"note": " Llamé a la empresa "}, headers=admin_headers)
    assert noted.status_code == 200 and noted.json()["data"]["events"][0]["note"] == "Llamé a la empresa"
    assert client.post(f"{CASES}/{case_id}/notes", json={"note": "   x  "}, headers=admin_headers).status_code == 422


def test_evidence_that_expired_or_the_bucket_cannot_serve(client, company_headers, admin_headers):
    _, case_id = open_case(client, company_headers)
    with SessionLocal() as db:
        items = db.scalars(select(FraudEvidence).order_by(FraudEvidence.id)).all()
        first, second, third = items[0], items[1], items[2]
        third.object_name = None
        db.commit()
        second_sha = swap_object(get_storage(), second.object_name)  # cifrado con otra llave
        db.execute(update(FraudEvidence).where(FraudEvidence.id == second.id).values(sha256=second_sha))
        db.commit()
    url = f"{CASES}/{case_id}/evidence"
    assert client.get(f"{url}/{third.id}", headers=admin_headers).json()["code"] == "FRAUD_EVIDENCE_NOT_FOUND"
    assert client.get(f"{url}/999", headers=admin_headers).status_code == 404
    unreadable = client.get(f"{url}/{second.id}", headers=admin_headers)
    assert unreadable.status_code == 404 and unreadable.json()["code"] == "FRAUD_EVIDENCE_NOT_FOUND"
    get_storage().down.add("get")
    down = client.get(f"{url}/{first.id}", headers=admin_headers)
    assert down.status_code == 503 and down.json()["code"] == "STORAGE_UNAVAILABLE"


# ---------------------------------------------------------------- retención (mantenimiento)


def test_expired_evidence_and_old_decided_cases_are_purged(client, company_headers, admin_headers):
    _, case_id = open_case(client, company_headers)
    client.post(f"{CASES}/{case_id}/decision", json={"status": "INCONCLUSIVE"}, headers=admin_headers)
    later = datetime.now(UTC) + timedelta(days=settings.FRAUD_EVIDENCE_RETENTION_DAYS + 1)
    with SessionLocal() as db:
        removed = fraud_case_service.purge(db, later, 2)
        assert db.scalar(select(func.count()).select_from(FraudEvidence)) == 0
        assert db.scalar(select(func.count()).select_from(StorageDeletion)) == 6  # salen del bucket
    assert removed == {"evidencia de casos de fraude": 6, "casos de fraude decididos": 0}
    much_later = datetime.now(UTC) + timedelta(days=settings.FRAUD_CASE_RETENTION_DAYS + 1)
    with SessionLocal() as db:
        assert fraud_case_service.purge(db, much_later, 10)["casos de fraude decididos"] == 1
    assert cases() == []


def test_a_failing_fraud_purge_does_not_stop_the_maintenance(monkeypatch, caplog):
    def broken(*_args):
        raise OperationalError("DELETE", {}, Exception("bloqueado"))

    monkeypatch.setattr(fraud_case_service, "purge", broken)
    with caplog.at_level(logging.ERROR):
        result = maintenance_service.purge_expired(SessionLocal(), now=datetime.now(UTC))
    assert "Falló la depuración de los casos de fraude" in caplog.text and "casos de fraude decididos" not in result


@pytest.mark.parametrize("scope", ["employee", "company"])
def test_deleting_an_employee_or_a_company_releases_its_evidence(client, company_headers, scope):
    open_case(client, company_headers)
    with SessionLocal() as db:
        case = db.scalars(select(FraudCase)).one()
        if scope == "employee":
            image_storage.release_employee_images(db, case.company_id, case.employee_id)
        else:
            image_storage.release_company_evidence(db, case.company_id)
        db.commit()
        queued = set(db.scalars(select(StorageDeletion.object_name)))
        names = set(db.scalars(select(FraudEvidence.object_name)))
    assert names and names <= queued and FRAUD_EVIDENCE.kind == "fraud-evidence"


def test_an_enrollment_with_a_photo_opens_its_own_case(client, company_headers):
    create_employee(client, company_headers)
    headers = login(client, "juan@empresa.com", "Empleado123")
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", b"exif:juan", "image/jpeg")) for i in range(3)] + turn_files(challenge)
    response = client.post(
        "/api/enrollment/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
    )
    assert response.json()["code"] == "IMAGE_NOT_FROM_CAMERA"
    assert cases()[0].kind == "INJECTION"
