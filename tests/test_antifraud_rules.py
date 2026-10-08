"""Antifraude, reglas puras (sin base de datos): motor de riesgo, dirección de cada cambio de la política, huella
perceptual (pHash) y el cociente rostro/fondo del destello."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.facial_recognition import phash
from app.facial_recognition.photometry import FACE_RATIO_CAP, FlashResponse
from app.models import RiskAction, RiskTier
from app.services import policy_rules, risk_rules
from app.services.risk_rules import Hit, RiskConfig, SignalSetting

FIXTURE = Path(__file__).parent / "fixtures" / "astronaut.png"
SIGNALS = {
    "SPOOF_PROB_LOW": SignalSetting("SPOOF_PROB_LOW", "PRESENTATION", 20, "ENFORCE", review_reason="CAPTURE"),
    "FLASH_FLAT": SignalSetting("FLASH_FLAT", "PRESENTATION", 50, "ENFORCE", review_reason="CAPTURE"),
    "MATCH_MARGIN_LOW": SignalSetting("MATCH_MARGIN_LOW", "BUDDY_PUNCHING", 10, "ENFORCE", review_reason="IDENTITY"),
    "CAMERA_LABEL_MISSING": SignalSetting("CAMERA_LABEL_MISSING", "INJECTION", 20, "OBSERVE", review_reason="CAMERA"),
    "LOCATION_EDGE": SignalSetting("LOCATION_EDGE", "LOCATION", 35, "ENFORCE", review_reason="LOCATION"),
    "LOCATION_ROUND_ACCURACY": SignalSetting(
        "LOCATION_ROUND_ACCURACY", "LOCATION", 35, "ENFORCE", review_reason="LOCATION"
    ),
    "REPLAY_PERCEPTUAL": SignalSetting("REPLAY_PERCEPTUAL", "REPLAY", 60, "OBSERVE", hard=True, review_reason="REPLAY"),
    "KNOWN_ATTACK": SignalSetting("KNOWN_ATTACK", "REPLAY", 80, "ENFORCE", hard=True, review_reason="KNOWN_ATTACK"),
    "COMPANY_UNDER_ATTACK": SignalSetting("COMPANY_UNDER_ATTACK", "OTHER", 15, "OFF", review_reason="ACTIVITY"),
}


def config(**changes) -> RiskConfig:
    values = {
        "enabled": True,
        "medium": 30,
        "high": 60,
        "critical": 80,
        "medium_action": "STEP_UP",
        "high_action": "REVIEW",
        "critical_action": "DENY",
        "fallback": "ALLOW",
        "family_cap": 60,
        "signals": SIGNALS,
    }
    return RiskConfig(**{**values, **changes})


# ---------------------------------------------------------------- motor de riesgo


def test_nothing_that_weighs_is_allowed_and_observed_signals_do_not_add_points():
    decision = risk_rules.decide(
        [Hit("CAMERA_LABEL_MISSING"), Hit("COMPANY_UNDER_ATTACK"), Hit("NOT_A_SIGNAL")], config()
    )
    assert (decision.score, decision.tier, decision.action) == (0, RiskTier.LOW, RiskAction.ALLOW)
    # Lo que solo se mide queda registrado con sus puntos (para calibrar y simular); lo apagado o desconocido, no.
    assert [(r.code, r.points, r.mode) for r in decision.reasons] == [("CAMERA_LABEL_MISSING", 20, "OBSERVE")]
    assert decision.enforced == ()


def test_each_tier_takes_its_configured_action():
    assert risk_rules.decide([Hit("SPOOF_PROB_LOW"), Hit("MATCH_MARGIN_LOW")], config()).action == RiskAction.STEP_UP
    high = risk_rules.decide([Hit("FLASH_FLAT", 1.0, 1.25), Hit("MATCH_MARGIN_LOW")], config())
    assert (high.score, high.tier, high.action) == (60, RiskTier.HIGH, RiskAction.REVIEW)
    critical = risk_rules.decide([Hit("FLASH_FLAT"), Hit("MATCH_MARGIN_LOW"), Hit("LOCATION_EDGE")], config())
    assert (critical.tier, critical.action) == (RiskTier.CRITICAL, RiskAction.DENY) and critical.hard is None
    # Los motivos, del que más pesó al que menos (y con su valor y umbral).
    assert [r.code for r in critical.reasons] == ["FLASH_FLAT", "LOCATION_EDGE", "MATCH_MARGIN_LOW"]
    assert high.reasons[0].as_dict() == {
        "code": "FLASH_FLAT",
        "points": 50,
        "mode": "ENFORCE",
        "kind": "PRESENTATION",
        "value": 1.0,
        "threshold": 1.25,
    }
    # Cualquier acción del catálogo, por nivel.
    assert risk_rules.decide([Hit("FLASH_FLAT")], config(medium_action="ALERT")).action == RiskAction.ALERT


def test_a_family_has_a_cap_so_one_noisy_source_does_not_decide_alone():
    hits = [Hit("LOCATION_EDGE"), Hit("LOCATION_ROUND_ACCURACY")]  # 35 + 35 del mismo tipo
    assert risk_rules.decide(hits, config()).score == 60
    assert risk_rules.decide(hits, config(family_cap=40)).score == 40
    assert risk_rules.score_of(risk_rules.reasons_of(hits, config()), 100) == 70


def test_an_enforced_hard_rule_denies_whatever_the_score_and_an_observed_one_does_not():
    denied = risk_rules.decide([Hit("KNOWN_ATTACK", 1.0)], config())
    assert (denied.score, denied.tier, denied.action, denied.hard) == (
        100,
        RiskTier.CRITICAL,
        RiskAction.DENY,
        "KNOWN_ATTACK",
    )
    observed = risk_rules.decide([Hit("REPLAY_PERCEPTUAL", 3.0, 8.0)], config())
    assert observed.action == RiskAction.ALLOW and observed.reasons[0].mode == "OBSERVE"


def test_a_completed_step_up_is_allowed_and_without_liveness_it_goes_to_review():
    hits = [Hit("SPOOF_PROB_LOW"), Hit("MATCH_MARGIN_LOW")]
    assert risk_rules.decide(hits, config(), step_up_done=True).action == RiskAction.ALLOW
    # Sin prueba de vida no hay otro reto que pedir: nadie se queda sin checar, queda en revisión.
    assert risk_rules.decide(hits, config(), can_step_up=False).action == RiskAction.REVIEW


def test_the_engine_can_be_turned_off_but_still_records_what_it_saw():
    decision = risk_rules.decide([Hit("KNOWN_ATTACK")], config(enabled=False))
    assert decision.action == RiskAction.ALLOW and decision.reasons[0].code == "KNOWN_ATTACK"


def test_the_company_only_sees_business_reasons_without_repeating():
    decision = risk_rules.decide([Hit("LOCATION_EDGE"), Hit("LOCATION_ROUND_ACCURACY"), Hit("FLASH_FLAT")], config())
    assert risk_rules.review_reasons(decision, config()) == ("CAPTURE", "LOCATION")


def test_a_stored_decision_can_be_replayed_with_another_configuration():
    stored = risk_rules.decide([Hit("FLASH_FLAT", 1.0, 1.25)], config())
    rows = [r.as_dict() for r in stored.reasons]
    assert risk_rules.replay(rows, config()).action == RiskAction.STEP_UP
    looser = config(signals={**SIGNALS, "FLASH_FLAT": SignalSetting("FLASH_FLAT", "PRESENTATION", 10, "ENFORCE")})
    assert risk_rules.replay(rows, looser).action == RiskAction.ALLOW
    assert risk_rules.replay(rows, config(), step_up_done=True).action == RiskAction.ALLOW


def test_the_configuration_has_a_stable_short_version():
    assert config().version() == config().version() and len(config().version()) == 12
    assert config().version() != config(medium=25).version()


# ---------------------------------------------------------------- dirección de un cambio de la política


def rank(catalog: str, code: str) -> int:
    orders = {
        "antispoof_levels": ["STANDARD", "HIGH", "MAXIMUM"],
        "flash_modes": ["OFF", "OBSERVE", "ENFORCE"],
        "risk_actions": ["ALLOW", "ALERT", "STEP_UP", "REVIEW", "DENY"],
        "employee_device_modes": ["OFF", "OBSERVE", "STEP_UP", "APPROVAL"],
        "signal_modes": ["OFF", "OBSERVE", "ENFORCE"],
    }
    return orders[catalog].index(code)


@pytest.mark.parametrize(
    ("field", "before", "after", "relaxes"),
    [
        ("anti_spoofing", True, False, True),
        ("anti_spoofing", False, True, False),
        ("qr_enabled", False, True, True),
        ("qr_only_attendance", True, False, False),
        ("min_confidence", 0.99999, 0.99, True),
        ("liveness_steps", 2, 3, False),
        ("liveness_timeout_seconds", 60, 90, True),
        ("duplicate_confidence", 0.99, 0.95, False),
        ("risk_medium_score", 30, 40, True),
        ("anti_spoofing_level", "HIGH", "STANDARD", True),
        ("flash_liveness", "OBSERVE", "ENFORCE", False),
        ("risk_high_action", "REVIEW", "ALERT", True),
        ("employee_device_mode", "OFF", "APPROVAL", False),
        ("risk_signals.FLASH_FLAT.mode", "ENFORCE", "OBSERVE", True),
        ("risk_signals.FLASH_FLAT.points", 30, 40, False),
        ("risk_signals.FLASH_FLAT.points", 30, 10, True),
        ("adaptive_learning", True, False, False),
        ("fraud_evidence", True, False, False),
        ("voice_verification", True, False, True),
    ],
)
def test_each_field_knows_which_way_is_safer(field, before, after, relaxes):
    assert policy_rules.relaxes(field, before, after, rank) is relaxes


def test_only_what_changes_is_in_the_diff_and_signals_are_flattened():
    current = {"anti_spoofing": True, "liveness_steps": 2}
    changes = policy_rules.diff(current, {"anti_spoofing": True, "liveness_steps": 3}, rank)
    assert [c.as_dict() for c in changes] == [{"field": "liveness_steps", "before": 2, "after": 3, "relaxes": False}]
    assert policy_rules.flatten_signals({"B": {"points": 5, "mode": "OFF"}, "A": {"mode": "ENFORCE"}}) == {
        "risk_signals.A.mode": "ENFORCE",
        "risk_signals.B.mode": "OFF",
        "risk_signals.B.points": 5,
    }


def test_presets_get_stricter_from_standard_to_maximum():
    standard, high, maximum = (policy_rules.PRESETS[code] for code in ("STANDARD", "HIGH", "MAXIMUM"))
    assert standard["risk_medium_score"] > high["risk_medium_score"] > maximum["risk_medium_score"]
    # El destello se retiró (2026-10-06): ningún nivel lo enciende.
    assert standard["flash_liveness"] == high["flash_liveness"] == maximum["flash_liveness"] == "OFF"
    assert maximum["anti_spoofing_level"] == "MAXIMUM"
    assert not any(policy_rules.relaxes(f, standard[f], high[f], rank) for f in standard if f != "risk_signals")
    assert not any(policy_rules.relaxes(f, high[f], maximum[f], rank) for f in high if f != "risk_signals")
    # Fase 2b (decisión del dueño, 2026-10-06): Máximo exige las pruebas de presencia; Estándar y Alto solo miden.
    presence = ("validator_signing", "validator_location", "site_codes")
    assert {standard[f] for f in presence} == {high[f] for f in presence} == {"OBSERVE"}
    assert {maximum[f] for f in presence} == {"ENFORCE"}


# ---------------------------------------------------------------- pHash


def test_the_perceptual_hash_survives_recompression_but_not_another_image():
    image = cv2.imread(str(FIXTURE))
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    original = phash.phash(gray)
    ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 75])
    assert ok
    recompressed = phash.phash(cv2.cvtColor(cv2.imdecode(jpeg, cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY))
    brighter = phash.phash(np.clip(gray.astype(np.int16) + 6, 0, 255).astype(np.uint8))
    assert phash.distance(original, recompressed) <= 8 and phash.distance(original, brighter) <= 8
    other = phash.phash(cv2.flip(gray, 0))
    assert phash.distance(original, other) > 8
    # Cabe en un BIGINT con signo y viaja en hexadecimal sin perder nada.
    assert -(1 << 63) <= original < 1 << 63
    assert phash.from_hex(phash.to_hex(original)) == original and len(phash.to_hex(-1)) == 16
    assert phash.signed((1 << 64) - 1) == -1 and phash.distance(-1, 0) == 64


# ---------------------------------------------------------------- cociente rostro/fondo del destello


def test_the_flash_ratio_separates_a_screen_from_a_real_face():
    def response(magnitude: float, background: float | None) -> FlashResponse:
        return FlashResponse(score=0.9, magnitude=magnitude, background_magnitude=background, pairs=3)

    assert response(0.012, 0.012).face_ratio == 1.0  # tinte global: pantalla o papel (P1)
    assert response(0.03, 0.005).face_ratio == 6.0  # el rostro, cerca de la pantalla, responde más
    assert response(0.03, 0.0).face_ratio == FACE_RATIO_CAP  # fondo inmóvil: el tope
    assert response(0.03, None).face_ratio is None  # el rostro llena el encuadre: sin fondo que medir
    assert response(0.0, 0.0).face_ratio is None  # sin respuesta
