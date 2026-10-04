"""Prueba de vida reforzada: movimientos al azar, destello de colores, métricas, autocalibración y refuerzo."""

import logging
import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import numpy as np
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.database import SessionLocal
from app.facial_recognition import FaceAnalysis, LivenessAction
from app.facial_recognition.photometry import (
    FLASH_PALETTE,
    FlashSample,
    emitted_chroma,
    flash_hex,
    flash_response,
    flash_sample,
)
from app.models import Employee, FaceAttemptMetric, SecurityThreshold
from app.services import face_security, face_signals, maintenance_service
from app.services.liveness_service import LivenessResponse, challenge_store, random_sequence
from app.services.policy_service import PolicyService, clear_policy_cache
from tests.conftest import approved_employee, flash_files, turn_files
from tests.test_capture_security import employee_id, history
from tests.test_policy import admin_policy, set_policy
from tests.test_units import face, image_bytes, pipeline

VERIFY = "/api/verification/face"
ADMIN_URL = "/api/admin/face-security"


def attempt(client, headers, *, flash: str | None = "flash-{color}:{person}", turn: str = "turn:juan", count=None):
    """Un intento con el destello (`flash`: plantilla de cada fotograma; None = no se envía)."""
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(2)]
    kind, _, person = turn.partition(":")
    files += turn_files(challenge, person, image=f"{kind}:{{person}}")
    if flash is not None:
        files += flash_files(challenge, "juan", image=flash)[:count]
    return client.post(VERIFY, data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers)


def metrics() -> list[FaceAttemptMetric]:
    with SessionLocal() as db:
        return list(db.query(FaceAttemptMetric).order_by(FaceAttemptMetric.id))


def company_id() -> int:
    with SessionLocal() as db:
        return db.query(Employee).first().company_id


def add_metrics(rows: int, **values) -> None:
    with SessionLocal() as db:
        cid = db.query(Employee).first().company_id
        db.add_all(FaceAttemptMetric(company_id=cid, **values) for _ in range(rows))
        db.commit()


# ---------------------------------------------------------------- reglas puras


def test_the_flash_follows_the_emitted_colors_only_on_a_real_face():
    skin = np.array([0.45, 0.33, 0.22])

    def sample(color: str, weight: float = 0.3) -> FlashSample:
        face_rgb = (1 - weight) * skin + weight * emitted_chroma(color)
        r, g, b = (float(v) for v in face_rgb)
        return FlashSample(face=(r, g, b), background=(0.45, 0.33, 0.22))

    colors = ("RED", "CYAN", "BLUE")
    real = flash_response([sample(c) for c in colors], colors)
    assert real.score > 0.9 and real.magnitude > 0.05 and real.pairs == 3 and real.conclusive(0.004)
    assert real.background_magnitude == 0.0  # el fondo, lejos de la pantalla, no respondió
    flat = flash_response([sample(c, 0.0) for c in colors], colors)
    assert flat.score == 0.0 and flat.magnitude == 0.0 and not flat.conclusive(0.004)
    assert flash_response([sample("BLUE"), sample("RED")], ("RED", "BLUE")).score < 0  # respondió al revés
    # Pares del mismo color no comparan nada; sin pares no hay medición (ni fondo).
    same = flash_response([sample("RED"), sample("RED")], ("RED", "RED"))
    assert (same.pairs, same.score, same.background_magnitude) == (0, 0.0, None)
    assert flash_response([FlashSample(face=(0.4, 0.3, 0.3), background=None)] * 2, colors).background_magnitude is None
    assert flash_hex("MAGENTA") == "#FF00FF" and set(FLASH_PALETTE) >= {"RED", "GREEN", "BLUE"}
    assert np.allclose(emitted_chroma("YELLOW"), [0.5, 0.5, 0.0])


def test_flash_sample_measures_the_face_and_the_sides_inside_the_frame():
    image = np.zeros((200, 300, 3), dtype=np.uint8)
    image[:, :] = (0, 0, 200)  # BGR: fondo rojo
    image[40:160, 100:200] = (40, 80, 160)  # rostro
    measured = flash_sample(image, (100, 40, 100, 120))
    assert measured.face[0] == pytest.approx(160 / 280, abs=1e-3)
    assert measured.background is not None and measured.background[0] == pytest.approx(1.0)
    # Rostro pegado a los bordes: sin franjas de fondo. Sin luz: cromaticidad en ceros.
    assert flash_sample(image, (0, 0, 300, 200)).background is None
    assert flash_sample(np.zeros((50, 50, 3), dtype=np.uint8), (0, 0, 50, 50)).face == (0.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        flash_sample(image, (400, 300, 50, 50))  # el rostro quedó fuera del fotograma


def test_the_pipeline_measures_a_flash_frame_without_embeddings():
    capture = pipeline([face()]).analyze_flash(image_bytes())
    assert capture.sample.face == pytest.approx((1 / 3, 1 / 3, 1 / 3), abs=1e-3)
    assert capture.sample.background is not None and capture.image_size == (320, 320)
    assert capture.face_box[2] == 150 and len(capture.capture_digest) == 64


def test_random_sequences_never_repeat_a_step_in_a_row():
    options = tuple(LivenessAction)
    for _ in range(200):
        sequence = random_sequence(options, 3)
        assert len(sequence) == 3 and all(a != b for a, b in pairwise(sequence))
    assert random_sequence(("RED",), 3) == ("RED", "RED", "RED")  # sin de dónde elegir, se repite


def test_the_attempt_signals_keep_numbers_only():
    signals = face_signals.begin("OBSERVE")
    now = datetime.now(UTC)
    signals.challenged(2, now - timedelta(seconds=7), now)
    signals.frontal = [FaceAnalysis(np.zeros(4), 0.9, 0.8, 1.0, 120.0, (0, 0, 1, 1), real_probability=math.nan)]
    assert face_signals.current() is signals and face_signals.finish() is signals
    assert face_signals.finish() is None and face_signals.current() is not signals  # fuera de un intento
    metric = signals.metric(1, success=True, reason=None)
    assert metric.response_seconds == 7.0 and metric.frontal_real_min is None  # NaN no se guarda
    assert (metric.quality_mean, metric.flash_score, metric.yaw_min) == (0.8, None, None)


# ---------------------------------------------------------------- reto y destello por la API


def test_a_challenge_brings_up_to_three_steps_the_flash_and_the_live_thresholds(client, company_headers):
    headers = approved_employee(client, company_headers)
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert len(challenge["actions"]) == 2 and len(challenge["flash"]) == settings.FACE_FLASH_COLORS
    assert challenge["flash_required"] is False and challenge["expires_in"] == 60
    assert challenge["min_yaw_ratio"] == settings.FACE_LIVENESS_MIN_YAW_RATIO
    assert challenge["min_pitch_delta"] == settings.FACE_LIVENESS_MIN_PITCH_DELTA
    assert challenge["min_closer_scale"] == settings.FACE_LIVENESS_MIN_CLOSER_SCALE
    set_policy(client, company_headers, liveness_steps=3, liveness_timeout_seconds=30, flash_liveness="OFF")
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert len(challenge["actions"]) == 3 and challenge["flash"] == [] and challenge["expires_in"] == 30
    assert len(challenge["instructions"]) == 3
    # Cada movimiento se cumple (también mirar arriba/abajo y acercarse) y el intento se mide.
    for _ in range(4):
        assert attempt(client, headers, flash=None).json()["data"]["verified"] is True
    assert all(m.success and m.steps == 3 and m.flash_mode == "OFF" for m in metrics()[-4:])


def test_policy_rejects_an_unknown_flash_mode(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    response = client.put(url, json={"flash_liveness": "SOMETIMES"}, headers=admin)
    assert response.status_code == 422 and response.json()["code"] == "INVALID_FLASH_MODE"


def test_while_observing_the_flash_is_measured_but_never_blocks(client, company_headers, caplog):
    headers = approved_employee(client, company_headers)
    assert attempt(client, headers).json()["data"]["verified"] is True
    measured = metrics()[-1]
    assert measured.flash_mode == "OBSERVE" and measured.flash_score is not None and measured.flash_score > 0.9
    assert measured.flash_magnitude is not None and measured.flash_background is not None
    # Lo que no se puede medir no bloquea: ni sin rostro, ni ilegible, ni una falla del motor, ni sin destello.
    with caplog.at_level(logging.INFO):
        for broken in ("flash-noface:{person}", "flash-broken:{person}", "flash-exif:{person}", "flash-crash:{person}"):
            assert attempt(client, headers, flash=broken).json()["data"]["verified"] is True, broken
        assert attempt(client, headers, flash=None).json()["data"]["verified"] is True
        assert attempt(client, headers, flash="flash-wrong-{color}:{person}").json()["data"]["verified"] is True
    assert "El motor falló al medir el destello" in caplog.text
    assert metrics()[-1].flash_score is not None and metrics()[-1].flash_score < 0
    assert metrics()[-2].flash_score is None  # sin destello: sin medición


def test_an_enforced_flash_must_be_sent_measurable_and_followed(client, company_headers):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, flash_liveness="ENFORCE", lockout_enabled=False)
    assert client.post("/api/face/challenge", headers=headers).json()["data"]["flash_required"] is True
    assert attempt(client, headers).json()["data"]["verified"] is True
    for missing in ({"flash": None}, {"count": 2}):
        response = attempt(client, headers, **missing)
        assert response.status_code == 422 and response.json()["code"] == "LIVENESS_REQUIRED", missing
    inconclusive = attempt(client, headers, flash="flash-none:{person}").json()["data"]
    assert inconclusive["verified"] is False and "reflejo de la pantalla" in inconclusive["message"]
    wrong = attempt(client, headers, flash="flash-wrong-{color}:{person}")
    assert wrong.status_code == 422 and wrong.json()["code"] == "FLASH_MISMATCH"
    assert attempt(client, headers, flash="flash-noface:{person}").json()["data"]["verified"] is False
    assert attempt(client, headers, flash="flash-broken:{person}").json()["data"]["verified"] is False
    assert attempt(client, headers, flash="flash-exif:{person}").json()["code"] == "IMAGE_NOT_FROM_CAMERA"
    assert attempt(client, headers, flash="flash-{color}-moved:{person}").json()["code"] == "CAPTURE_INCONSISTENT"
    crash = attempt(client, headers, flash="flash-crash:{person}")
    assert crash.status_code == 503 and crash.json()["code"] == "FACE_PROCESSING_ERROR"
    reasons = [reason for _, reason in history(client, company_headers, employee_id(client, headers))]
    assert "FLASH_MISMATCH" in reasons and "FLASH_INCONCLUSIVE" in reasons
    assert {m.reason for m in metrics()} >= {"FLASH_MISMATCH", "FLASH_INCONCLUSIVE", None}


def test_a_flash_answer_for_a_challenge_without_flash_is_rejected(client, company_headers):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, flash_liveness="OFF")
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", ("f.jpg", b"face:juan", "image/jpeg")), *turn_files(challenge)]
    files.append(("flash_image", ("c.jpg", b"flash-RED:juan", "image/jpeg")))
    response = client.post(VERIFY, data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers)
    assert response.status_code == 422 and response.json()["code"] == "LIVENESS_REQUIRED"


def test_a_challenge_of_another_user_or_expired_is_not_served(client, company_headers):
    headers = approved_employee(client, company_headers)
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    with SessionLocal() as db:
        owner = db.query(Employee).first().user_id
        assert challenge_store.consume(db, challenge["challenge_id"], owner + 999) is None
        issued = challenge_store.issue(db, owner, steps=1, lifetime_seconds=-1)
        assert challenge_store.consume(db, issued.id, owner) is None  # ya venció
        fresh = challenge_store.issue(db, owner, steps=1, lifetime_seconds=60, flash=2)
        response = LivenessResponse(challenge_id=fresh.id, steps=(b"x",), flash=(b"a",))
        with pytest.raises(Exception) as error:
            challenge_store.require(db, owner, response, required=True)
        assert getattr(error.value, "code", None) == "LIVENESS_REQUIRED"


def test_the_step_threshold_follows_the_platform_calibration(client, company_headers, monkeypatch):
    """Un umbral endurecido por la plataforma se exige en el servidor y se le envía a la app."""
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, flash_liveness="OFF", liveness_steps=1)
    with SessionLocal() as db:
        for key, value in (("LIVENESS_YAW", 0.27), ("LIVENESS_PITCH", 0.13), ("LIVENESS_CLOSER", 1.44)):
            db.add(SecurityThreshold(key=key, value=value, samples=500, computed_at=datetime.now(UTC)))
        db.commit()
    face_security.clear_threshold_cache()
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert (challenge["min_yaw_ratio"], challenge["min_pitch_delta"], challenge["min_closer_scale"]) == (
        0.27,
        0.13,
        1.44,
    )
    # Lo que la persona apenas mueve (lo del simulador) ya no alcanza.
    result = attempt(client, headers, flash=None).json()["data"]
    assert result["verified"] is False and "movimiento solicitado" in result["message"]
    # Si después se baja el tope en la configuración, lo guardado se vuelve a acotar.
    monkeypatch.setattr(settings, "FACE_LIVENESS_MAX_YAW_RATIO", 0.2)
    face_security.clear_threshold_cache()
    with SessionLocal() as db:
        assert face_security.thresholds(db).min_yaw_ratio == 0.2
    monkeypatch.setattr(settings, "FACE_AUTOCALIBRATION_ENABLED", False)
    face_security.clear_threshold_cache()
    with SessionLocal() as db:
        assert face_security.thresholds(db).min_yaw_ratio == settings.FACE_LIVENESS_MIN_YAW_RATIO


# ---------------------------------------------------------------- autocalibración y refuerzo


def test_autocalibration_only_tightens_within_floor_and_cap(client, company_headers, monkeypatch):
    approved_employee(client, company_headers)
    monkeypatch.setattr(settings, "FACE_AUTOCALIBRATION_MIN_SAMPLES", 20)
    now = datetime.now(UTC)
    with SessionLocal() as db:
        assert face_security.recalibrate(db, now) == 5  # primera vez: los cinco quedan en su piso
        assert face_security.thresholds(db).min_yaw_ratio == settings.FACE_LIVENESS_MIN_YAW_RATIO
    add_metrics(
        30,
        success=True,
        yaw_min=0.3,
        pitch_min=0.2,
        closer_min=1.5,
        flash_score=0.9,
        flash_magnitude=0.01,
        frontal_real_min=0.95,
    )
    add_metrics(30, success=False, reason="SPOOF_DETECTED", yaw_min=0.01, frontal_real_min=0.01)
    with SessionLocal() as db:
        assert face_security.recalibrate(db, now) == 5
        limits = face_security.thresholds(db)
        assert limits.min_yaw_ratio == pytest.approx(0.255)  # 0.30 × 0.85, bajo el tope
        assert limits.min_pitch_delta == settings.FACE_LIVENESS_MAX_PITCH_DELTA  # topado
        assert limits.min_closer_scale == pytest.approx(1.425)  # 1 + 0.5 × 0.85
        assert limits.flash_min_score == settings.FACE_FLASH_MAX_SCORE
        assert limits.min_real_probability == settings.FACE_AUTOCALIBRATION_MAX_REAL
        assert face_security.recalibrate(db, now) == 0  # idempotente
        # El piso de rostro real endurece el nivel de anti-spoofing de la empresa.
        clear_policy_cache()
        assert PolicyService(db, company_id()).current().antispoof_threshold == settings.FACE_AUTOCALIBRATION_MAX_REAL


def test_recalibration_runs_from_maintenance_only_when_due(client, company_headers, monkeypatch):
    approved_employee(client, company_headers)
    now = datetime.now(UTC)
    with SessionLocal() as db:
        assert face_security.recalibrate_if_due(db, now) == 5
        assert face_security.recalibrate_if_due(db, now + timedelta(hours=1)) == 0  # aún vigente
        assert maintenance_service.purge_expired(db, now=now + timedelta(days=1))["umbrales recalibrados"] == 0
        monkeypatch.setattr(settings, "FACE_AUTOCALIBRATION_ENABLED", False)
        assert face_security.recalibrate_if_due(db, now + timedelta(days=2)) == 0


def test_a_failing_recalibration_does_not_stop_maintenance(monkeypatch, caplog):
    def broken(db, now):
        raise SQLAlchemyError("bloqueo")

    monkeypatch.setattr(maintenance_service, "recalibrate_if_due", broken)
    with SessionLocal() as db, caplog.at_level(logging.ERROR):
        assert maintenance_service.purge_expired(db)["umbrales recalibrados"] == 0
    assert "Falló la autocalibración" in caplog.text


def test_old_attempt_metrics_are_purged(client, company_headers):
    approved_employee(client, company_headers)
    old = datetime.now(UTC) - timedelta(days=settings.FACE_METRICS_RETENTION_DAYS + 1)
    add_metrics(2, success=True, created_at=old)
    add_metrics(1, success=True)
    with SessionLocal() as db:
        assert maintenance_service.purge_expired(db)["métricas de intentos faciales"] == 2
    assert len(metrics()) == 1


def test_a_company_under_attack_gets_the_longest_challenge(client, company_headers):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, liveness_steps=1)
    assert len(client.post("/api/face/challenge", headers=headers).json()["data"]["actions"]) == 1
    add_metrics(settings.FACE_ESCALATION_MIN_ATTACKS, success=False, reason="REPLAY_DETECTED")
    assert len(client.post("/api/face/challenge", headers=headers).json()["data"]["actions"]) == 3


def test_the_admin_sees_and_recalibrates_the_platform_security(client, company_headers, admin_headers, monkeypatch):
    approved_employee(client, company_headers)
    add_metrics(settings.FACE_ESCALATION_MIN_ATTACKS, success=False, reason="SPOOF_DETECTED")
    add_metrics(3, success=True, flash_score=0.8, flash_magnitude=0.02)
    add_metrics(1, success=True, flash_score=0.1, flash_magnitude=0.001)
    overview = client.get(ADMIN_URL, headers=admin_headers).json()["data"]
    assert [t["key"] for t in overview["thresholds"]] == [s.key for s in face_security.SIGNALS]
    assert all(t["samples"] == 0 and t["computed_at"] is None and not t["raised"] for t in overview["thresholds"])
    assert overview["reinforced"] == [
        {"company_id": company_id(), "name": overview["reinforced"][0]["name"], "attacks": 5}
    ]
    assert overview["flash"] == {
        "measured": 4,
        "conclusive": 3,
        "inconclusive": 1,
        "score_median": 0.8,
        "score_p10": 0.8,
        "magnitude_median": 0.02,
    }
    first = client.post(f"{ADMIN_URL}/recalibrate", headers=admin_headers).json()
    assert first["code"] == "THRESHOLDS_RECALIBRATED" and "5 cambiaron" in first["message"]
    again = client.post(f"{ADMIN_URL}/recalibrate", headers=admin_headers).json()
    assert again["message"] == "Umbrales recalculados: sin cambios"
    assert all(t["computed_at"] for t in again["data"]["thresholds"])
    monkeypatch.setattr(settings, "FACE_AUTOCALIBRATION_MIN_SAMPLES", 20)
    add_metrics(25, success=True, yaw_min=0.3)
    raised = client.post(f"{ADMIN_URL}/recalibrate", headers=admin_headers).json()["data"]["thresholds"][0]
    assert raised["raised"] is True and raised["samples"] == 25
    assert client.get(ADMIN_URL, headers=company_headers).status_code == 403
