"""Motor de riesgo en los flujos reales (fase 1 del antifraude): cada señal, sus acciones (un paso más, en revisión,
negar), las reglas duras (reenvío perceptual, ataque conocido), su respaldo si falla y "en revisión" en la asistencia
(la empresa confirma o rechaza)."""

import logging
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.facial_recognition import FaceAnalysis
from app.models import (
    AttackSignature,
    CaptureTrace,
    FaceAttemptMetric,
    FaceEmbedding,
    FraudCase,
    RiskAssessment,
    VerificationLog,
)
from app.services import attack_signatures
from app.services.attack_signatures import capture_value
from app.services.policy_service import PolicySnapshot
from app.services.risk_engine import Match, RiskEngine
from tests.conftest import _look, approved_employee, flash_files, turn_files
from tests.test_attendance import act, clock, recorded, worker  # noqa: F401 (fixtures)
from tests.test_policy import set_policy
from tests.test_validators import identify_face, validator_headers

VERIFY = "/api/verification/face"


def verify(client, headers, *, frontal=b"face:juan", camera: str | None = "Cámara FaceTime HD", challenge=None):
    """Verificación 1:1 con su reto (y el destello que pida)."""
    challenge = challenge or client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", frontal, "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "juan") + flash_files(challenge, "juan")
    data = {"challenge_id": challenge["challenge_id"], **({"camera_label": camera} if camera else {})}
    return client.post(VERIFY, data=data, files=files, headers=headers)


def signals(client, company_headers, **settings_by_code):
    """Ajusta señales del motor en la política de la empresa (como el ADMIN)."""
    return set_policy(client, company_headers, risk_signals=settings_by_code)


def assessments() -> list[RiskAssessment]:
    with SessionLocal() as db:
        return list(db.scalars(select(RiskAssessment).order_by(RiskAssessment.id)))


def last_reasons() -> dict[str, dict]:
    return {r["code"]: r for r in assessments()[-1].reasons}


# ---------------------------------------------------------------- permitir y registrar lo medido


def test_a_clean_attempt_is_allowed_recorded_linked_and_remembered(client, company_headers):
    set_policy(client, company_headers, flash_liveness="OBSERVE", flash_paced=False)  # retirado: se enciende aquí
    headers = approved_employee(client, company_headers)
    response = verify(client, headers)
    assert response.json()["data"]["verified"] is True and response.json()["data"]["review"] is False
    [assessment] = assessments()
    assert (assessment.score, assessment.tier, assessment.action, assessment.fallback) == (0, "LOW", "ALLOW", False)
    assert assessment.engine == "1.2.0" and len(assessment.policy_version) == 12
    with SessionLocal() as db:
        log = db.scalars(select(VerificationLog).order_by(VerificationLog.id.desc())).first()
        metric = db.scalars(select(FaceAttemptMetric).order_by(FaceAttemptMetric.id.desc())).first()
        traces = db.scalars(select(CaptureTrace)).all()
    assert assessment.verification_log_id == log.id == metric.verification_log_id  # D7: enlazados
    assert metric.flash_ratio is not None and metric.flash_ratio > 1.25  # el rostro responde más que el fondo
    assert len(traces) == 2 and all(t.embedding_encrypted and t.dimension == 128 for t in traces)
    assert client.get("/api/admin/fraud-cases/count", headers=company_headers).status_code == 403


def test_signals_measured_in_observe_mode_are_recorded_without_points(client, company_headers):
    set_policy(client, company_headers, flash_liveness="OBSERVE", flash_paced=False)  # retirado: se enciende aquí
    headers = approved_employee(client, company_headers)
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", b"lowreal:juan", "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "juan") + flash_files(challenge, "juan", image="flash-flat-{color}:{person}")
    response = client.post(VERIFY, data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers)
    assert response.json()["data"]["verified"] is True
    reasons = last_reasons()
    assert reasons["CAMERA_LABEL_MISSING"]["mode"] == "OBSERVE"
    assert reasons["FLASH_FLAT"] == {**reasons["FLASH_FLAT"], "mode": "OBSERVE", "value": 1.0, "threshold": 1.25}
    assert reasons["SPOOF_PROB_LOW"] == {**reasons["SPOOF_PROB_LOW"], "mode": "ENFORCE", "points": 20, "value": 0.07}
    assert assessments()[-1].score == 20 and assessments()[-1].action == "ALLOW"
    # El destello sin seguir los colores (solo se mide): respuesta débil.
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "juan") + flash_files(challenge, "juan", image="flash-wrong-{color}:{person}")
    data = {"challenge_id": challenge["challenge_id"], "camera_label": "Cam"}
    assert client.post(VERIFY, data=data, files=files, headers=headers).json()["data"]["verified"] is True
    assert "FLASH_WEAK" in last_reasons()


def test_the_flat_flash_is_suspicious_when_the_flash_is_enforced(client, company_headers):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, flash_liveness="ENFORCE")
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "juan") + flash_files(challenge, "juan", image="flash-flat-{color}:{person}")
    flat = client.post(VERIFY, data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers)
    assert flat.status_code == 422 and flat.json()["code"] == "FLASH_FLAT"
    assert flat.json()["errors"][0]["details"] == {"ratio": 1.0, "required": 1.25}
    assert verify(client, headers).json()["data"]["verified"] is True  # un rostro real sí pasa


# ---------------------------------------------------------------- un paso más (riesgo medio)


def test_medium_risk_asks_for_one_more_step_without_repeating_the_scan(client, company_headers):
    headers = approved_employee(client, company_headers)
    signals(client, company_headers, CAMERA_LABEL_MISSING={"mode": "ENFORCE", "points": 30})
    asked = verify(client, headers, camera=None)
    assert asked.status_code == 422 and asked.json()["code"] == "STEP_UP_REQUIRED"
    step_up = asked.json()["errors"][0]["details"]["challenge"]
    assert step_up["step_up"] is True and step_up["flash_required"] is False and len(step_up["actions"]) == 3
    # El destello se retiró de la experiencia (decisión del dueño, 2026-10-06): tampoco en el paso extra.
    assert step_up["flash"] == [] and step_up["flash_pace"] is None
    with SessionLocal() as db:
        assert (
            db.scalars(select(VerificationLog.reason).order_by(VerificationLog.id.desc())).first() == "STEP_UP_REQUIRED"
        )
    # El paso extra se responde con sus tres movimientos (ya sin destello); completo, pasa (sigue sin cámara: el riesgo
    # medio ya se atendió con el paso extra).
    done = verify(client, headers, camera=None, challenge=step_up)
    assert done.json()["data"]["verified"] is True and assessments()[-1].step_up is True
    # Un paso más no es un fallo: no cuenta para el bloqueo ni abre un caso.
    with SessionLocal() as db:
        assert db.scalar(select(FraudCase.id)) is None


def test_without_liveness_medium_risk_is_recorded_in_review(client, company_headers):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, liveness_challenge=False)
    signals(client, company_headers, CAMERA_LABEL_MISSING={"mode": "ENFORCE", "points": 30})
    files = [("images", ("f.jpg", b"face:juan", "image/jpeg"))]
    data = client.post(VERIFY, files=files, headers=headers).json()["data"]
    assert data["verified"] is True and data["review"] is True
    assert assessments()[-1].action == "REVIEW"


# ---------------------------------------------------------------- negar (riesgo crítico y reglas duras)


def test_critical_risk_is_denied_counts_for_the_lockout_and_opens_a_case(client, company_headers):
    headers = approved_employee(client, company_headers)
    # Una sola familia no llega al nivel crítico (tope RISK_FAMILY_MAX_POINTS = 60): hacen falta dos fuentes.
    signals(client, company_headers, CAMERA_LABEL_MISSING={"mode": "ENFORCE", "points": 80})
    assert verify(client, headers, camera=None).json()["data"]["review"] is True
    denied = verify(client, headers, frontal=b"lowreal:juan", camera=None)
    assert denied.status_code == 422 and denied.json()["code"] == "RISK_DENIED"
    assert "No se pudo confirmar tu identidad" in denied.json()["message"]  # nunca qué lo delató
    with SessionLocal() as db:
        case = db.scalars(select(FraudCase)).one()
    assert (case.kind, case.reason, case.status, case.max_score, case.tier, case.attempts) == (
        "INJECTION",
        "CAMERA_LABEL_MISSING",
        "OPEN",
        80,
        "CRITICAL",
        2,  # el "en revisión" de antes también abrió (y sumó) el caso
    )


def test_a_modified_replay_is_detected_by_its_perceptual_hash(client, company_headers):
    headers = approved_employee(client, company_headers)
    assert verify(client, headers, frontal=b"face:juan@foto").json()["data"]["verified"] is True
    # Solo se mide: pasa, pero queda registrado (y no se aprende de esa captura).
    with SessionLocal() as db:
        learned = db.query(FaceEmbedding).filter(FaceEmbedding.learned.is_(True)).count()
    assert verify(client, headers, frontal=b"face:juan@foto^3").json()["data"]["verified"] is True
    reason = last_reasons()["REPLAY_PERCEPTUAL"]
    assert reason["mode"] == "OBSERVE" and reason["value"] == 3.0 and reason["threshold"] == 8.0
    with SessionLocal() as db:
        assert db.query(FaceEmbedding).filter(FaceEmbedding.learned.is_(True)).count() == learned
    # Exigida, es una regla dura: niega (y cuenta como intento sospechoso).
    signals(client, company_headers, REPLAY_PERCEPTUAL={"mode": "ENFORCE"})
    replay = verify(client, headers, frontal=b"face:juan@foto^5")
    assert replay.status_code == 422 and replay.json()["code"] == "REPLAY_PERCEPTUAL"
    # Una captura distinta (otro aspecto) no se confunde.
    assert verify(client, headers, frontal=b"face:juan@otra").json()["data"]["verified"] is True


def test_an_unreadable_trace_never_matches(client, company_headers):
    headers = approved_employee(client, company_headers)
    signals(client, company_headers, REPLAY_PERCEPTUAL={"mode": "ENFORCE"})
    assert verify(client, headers, frontal=b"face:juan@foto").json()["data"]["verified"] is True
    with SessionLocal() as db:
        for trace in db.scalars(select(CaptureTrace)):
            trace.embedding_encrypted = b"ilegible"
        db.commit()
    assert verify(client, headers, frontal=b"face:juan@foto^1").json()["data"]["verified"] is True


def test_a_known_attack_signature_is_recognized(client, company_headers):
    headers = approved_employee(client, company_headers)
    face, frame = _look(b"face:juan@ataque")
    with SessionLocal() as db:
        db.add(
            AttackSignature(
                kind="CAPTURE_PHASH",
                value=capture_value(face, frame),
                company_id=None,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        db.commit()
    attack_signatures.signature_cache.clear()
    assert verify(client, headers, frontal=b"face:juan@ataque^2").json()["data"]["verified"] is True
    assert last_reasons()["KNOWN_ATTACK"]["mode"] == "OBSERVE"
    signals(client, company_headers, KNOWN_ATTACK={"mode": "ENFORCE"})
    denied = verify(client, headers, frontal=b"face:juan@ataque^1")
    assert denied.status_code == 422 and denied.json()["code"] == "KNOWN_ATTACK"
    with SessionLocal() as db:
        signature = db.scalars(select(AttackSignature)).one()
    assert signature.hits == 2 and signature.last_hit_at is not None


def test_a_reinforced_company_adds_its_points(client, company_headers, monkeypatch):
    from app.services import identity_core

    headers = approved_employee(client, company_headers)
    monkeypatch.setattr(identity_core, "under_attack", lambda *_: True)
    assert verify(client, headers).json()["data"]["verified"] is True
    assert last_reasons()["COMPANY_UNDER_ATTACK"]["points"] == 15


def test_a_tight_match_adds_its_points(client, company_headers):
    headers = approved_employee(client, company_headers)
    assert verify(client, headers, frontal=b"face:juan~0.665~cerca").json()["data"]["verified"] is True
    assert last_reasons()["MATCH_MARGIN_LOW"]["mode"] == "ENFORCE"


# ---------------------------------------------------------------- respaldo si el motor falla


def test_if_the_engine_fails_the_fallback_applies_and_the_error_is_recorded(
    client, company_headers, monkeypatch, caplog
):
    headers = approved_employee(client, company_headers)

    def broken(*_args, **_kwargs):
        raise RuntimeError("dato corrupto")

    monkeypatch.setattr(RiskEngine, "_hits", broken)
    with caplog.at_level(logging.ERROR):
        assert verify(client, headers).json()["data"]["verified"] is True
    assert "El motor de riesgo falló" in caplog.text
    assert assessments()[-1].fallback is True and assessments()[-1].action == "ALLOW"
    set_policy(client, company_headers, risk_fallback_action="STEP_UP")
    assert verify(client, headers).json()["code"] == "STEP_UP_REQUIRED"


def test_the_engine_can_be_turned_off(client, company_headers):
    headers = approved_employee(client, company_headers)
    signals(client, company_headers, CAMERA_LABEL_MISSING={"mode": "ENFORCE", "points": 90})
    set_policy(client, company_headers, risk_engine=False)
    assert verify(client, headers, camera=None).json()["data"]["verified"] is True


def test_captures_without_perceptual_hash_are_skipped():
    """Una captura sin huella perceptual (motor que no la calcula) no se compara ni se bloquea."""
    analysis = FaceAnalysis(
        embedding=np.ones(128, dtype=np.float32),
        detection_score=0.9,
        quality_score=0.9,
        sharpness=100.0,
        brightness=120.0,
        face_box=(0, 0, 100, 100),
    )
    with SessionLocal() as db:
        engine = RiskEngine(db, 1, PolicySnapshot())
        assert engine._replay(1, [analysis]) == [] and engine._known_attack([analysis]) == []
        outcome = engine.evaluate(employee_id=None, frontal=[analysis], match=Match((), 0.6))
    assert outcome.action == "ALLOW" and outcome.clean is True


# ---------------------------------------------------------------- "en revisión" en la asistencia


def test_high_risk_attendance_is_recorded_in_review_for_the_company(client, company_headers, worker, clock):  # noqa: F811
    headers, day = worker["headers"], worker["day"]
    signals(client, company_headers, LOCATION_ROUND_ACCURACY={"mode": "ENFORCE", "points": 60})
    clock(day, "07:55")
    response = act(client, headers, "check-in", accuracy=10)
    session = recorded(response)
    assert response.json()["message"] == "Entrada registrada: queda en revisión de tu empresa"
    assert response.json()["data"]["verification"]["review"] is True
    assert session["review_status"] == "PENDING" and session["review_reasons"] == []  # el empleado no ve el motivo
    count = client.get("/api/attendance/reviews/count", headers=company_headers).json()["data"]
    assert count == {"pending": 1}
    inbox = client.get("/api/attendance/sessions?in_review=true", headers=company_headers).json()["data"]
    assert inbox["total"] == 1 and inbox["items"][0]["review_reasons"] == ["LOCATION"]
    detail = client.get(f"/api/attendance/sessions/{session['id']}", headers=company_headers).json()["data"]
    assert detail["events"][0]["under_review"] is True

    url = f"/api/attendance/sessions/{session['id']}/review"
    no_note = client.post(url, json={"decision": "REJECTED"}, headers=company_headers)
    assert no_note.status_code == 422 and no_note.json()["code"] == "REVIEW_NOTE_REQUIRED"
    invalid = client.post(url, json={"decision": "PENDING"}, headers=company_headers)
    assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_REVIEW_DECISION"
    rejected = client.post(
        url, json={"decision": "REJECTED", "note": "  No estaba en la planta "}, headers=company_headers
    )
    assert rejected.status_code == 200 and rejected.json()["code"] == "ATTENDANCE_REVIEWED"
    assert rejected.json()["data"]["review_status"] == "REJECTED"
    assert rejected.json()["data"]["review_note"] == "No estaba en la planta"
    again = client.post(url, json={"decision": "CONFIRMED"}, headers=company_headers)
    assert again.status_code == 409 and again.json()["code"] == "ATTENDANCE_REVIEW_NOT_PENDING"
    missing = client.post(
        "/api/attendance/sessions/999/review", json={"decision": "CONFIRMED"}, headers=company_headers
    )
    assert missing.status_code == 404
    assert client.get("/api/attendance/reviews/count", headers=company_headers).json()["data"] == {"pending": 0}
    mine = client.get("/api/me/attendance/history", headers=headers).json()["data"]["items"][0]
    assert mine["review_status"] == "REJECTED" and mine["review_note"] == "No estaba en la planta"
    assert mine["review_reasons"] == []
    # Un nuevo registro con riesgo alto vuelve a dejar la jornada en revisión (con sus motivos acumulados).
    clock(day, "16:05")
    out = recorded(act(client, headers, "check-out", accuracy=10))
    assert out["review_status"] == "PENDING"
    confirmed = client.post(url, json={"decision": "CONFIRMED"}, headers=company_headers).json()["data"]
    assert confirmed["review_status"] == "CONFIRMED" and confirmed["review_note"] is None


def test_the_edge_of_the_site_is_a_location_signal(client, company_headers, worker, clock):  # noqa: F811
    from tests.test_shifts import POINT

    headers, day = worker["headers"], worker["day"]
    clock(day, "07:55")
    edge = (POINT[0] + 0.00085, POINT[1])  # ~95 m del centro de un sitio de 100 m
    recorded(act(client, headers, "check-in", at=edge, accuracy=7.3))
    reasons = last_reasons()
    assert reasons["LOCATION_EDGE"]["mode"] == "OBSERVE" and reasons["LOCATION_EDGE"]["value"] > 0.9
    assert "LOCATION_ROUND_ACCURACY" not in reasons


def test_a_validator_identification_can_also_be_in_review(client, company_headers, worker, clock):  # noqa: F811
    validator = validator_headers(client, company_headers, mode="FACE")
    signals(client, company_headers, CAMERA_LABEL_MISSING={"mode": "ENFORCE", "points": 60})
    clock(worker["day"], "07:55")
    data = identify_face(client, validator, "ana").json()["data"]
    assert data["verified"] is True and data["review"] is True
    assert data["attendance"]["message"] == "Entrada registrada: queda en revisión de tu empresa a las 07:55."
    sessions = client.get("/api/attendance/sessions?in_review=true", headers=company_headers).json()["data"]
    assert sessions["items"][0]["review_reasons"] == ["CAMERA"]


@pytest.mark.parametrize(("points", "kind", "code"), [(30, "face", "STEP_UP_REQUIRED"), (60, "lowreal", "RISK_DENIED")])
def test_the_validator_steps_up_or_denies_too(client, company_headers, points, kind, code):
    from tests.test_validators import approved

    approved(client, company_headers, "ana", number="EMP-001")
    validator = validator_headers(client, company_headers, mode="FACE")
    signals(client, company_headers, CAMERA_LABEL_MISSING={"mode": "ENFORCE", "points": points})
    response = identify_face(client, validator, "ana", kind=kind)
    assert response.status_code == 422 and response.json()["code"] == code
