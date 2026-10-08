"""Gobierno de la política de verificación (fase 0 del antifraude): historial, regla de dos personas para relajar,
niveles predefinidos, el motor de riesgo en la política del ADMIN y la simulación."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, update

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import PolicyChange, RiskAssessment, VerificationPolicy
from app.services.policy_service import PolicySnapshot
from app.services.risk_engine import config_for
from app.services.risk_rules import Hit, decide
from tests.conftest import ADMIN_EMAIL, SECOND_ADMIN_EMAIL, second_admin
from tests.test_policy import admin_policy, set_policy

SETTINGS_URL = "/api/settings/verification"


@pytest.fixture(autouse=True)
def two_person_rule_on(monkeypatch):
    """La regla está apagada por omisión (un único ADMIN: todo aplica al momento, decisión del dueño 2026-10-06); estas
    pruebas la encienden para seguir probando el mecanismo completo (pendiente, aprobar, rechazar, vencer)."""
    monkeypatch.setattr(settings, "POLICY_TWO_PERSON_RULE", True)


def put(client, url, admin, **changes):
    response = client.put(url, json=changes, headers=admin)
    assert response.status_code == 200, response.text
    return response.json()


def history(client, url, admin, query: str = "") -> list[dict]:
    response = client.get(f"{url}/changes{query}", headers=admin)
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]


def company_id_of(client, company_headers) -> int:
    return client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]


def test_the_admin_sees_the_risk_engine_and_the_company_does_not(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    policy = client.get(url, headers=admin).json()["data"]
    assert policy["risk_engine"] is True and policy["qr_only_attendance"] is False  # D4: empresa nueva
    assert (policy["risk_medium_score"], policy["risk_high_score"], policy["risk_critical_score"]) == (30, 60, 80)
    assert (policy["risk_medium_action"], policy["risk_high_action"], policy["risk_critical_action"]) == (
        "STEP_UP",
        "REVIEW",
        "DENY",
    )
    assert policy["employee_device_mode"] == "OBSERVE" and policy["duplicate_confidence"] == 0.99
    assert policy["pending_changes"] == 0 and policy["two_person_rule"] is True and policy["preset"] is None
    flat = next(s for s in policy["risk_signals"] if s["code"] == "FLASH_FLAT")
    assert flat == {**flat, "mode": "OBSERVE", "default_mode": "OBSERVE", "points": 30, "hard": False}
    assert flat["confirmed"] == 0 and flat["false_positive"] == 0
    # La empresa y su personal leen la política sin el motor de riesgo (no se le enseña qué se mide).
    company = client.get(SETTINGS_URL, headers=company_headers).json()["data"]
    assert "risk_signals" not in company and "risk_engine" not in company and company["qr_only_attendance"] is False


def test_tightening_applies_now_and_stays_in_the_history(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    body = put(client, url, admin, liveness_steps=3, risk_high_score=55, reason="Ataques en la sucursal")
    assert body["code"] == "POLICY_UPDATED"
    assert body["data"]["policy"]["liveness_steps"] == 3 and body["data"]["policy"]["risk_high_score"] == 55
    change = body["data"]["change"]
    assert change["status"] == "APPLIED" and change["relaxes"] is False and change["reason"] == "Ataques en la sucursal"
    assert change["requested_by"] == ADMIN_EMAIL and change["requested_by_me"] is True and change["expires_at"] is None
    assert {c["field"]: (c["before"], c["after"]) for c in change["changes"]} == {
        "liveness_steps": (2, 3),
        "risk_high_score": (60, 55),
    }
    # Cambiar el corte del motor adjunta la simulación de los últimos días.
    assert change["simulation"]["evaluated"] == 0 and change["simulation"]["days"] == settings.RISK_SIMULATION_DAYS
    assert [c["status"] for c in history(client, url, admin)] == ["APPLIED"]
    unchanged = put(client, url, admin, liveness_steps=3)
    assert unchanged["code"] == "POLICY_UNCHANGED" and unchanged["data"]["change"] is None


def test_relaxing_waits_for_another_admin(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    body = put(client, url, admin, anti_spoofing=False, liveness_steps=3)
    assert body["code"] == "POLICY_CHANGE_PENDING"
    change = body["data"]["change"]
    assert change["status"] == "PENDING" and change["relaxes"] is True and change["expires_at"]
    assert change["simulation"] is None  # no toca el motor de riesgo
    # Todo el cambio espera (también lo que endurecía): la política sigue igual.
    policy = body["data"]["policy"]
    assert policy["anti_spoofing"] is True and policy["liveness_steps"] == 2 and policy["pending_changes"] == 1

    approve = f"{url}/changes/{change['id']}/approve"
    own = client.post(approve, headers=admin)
    assert own.status_code == 409 and own.json()["code"] == "POLICY_SELF_APPROVAL"
    other = second_admin(client)
    approved = client.post(approve, headers=other)
    assert approved.status_code == 200 and approved.json()["code"] == "POLICY_CHANGE_APPROVED"
    applied = approved.json()["data"]
    assert applied["policy"]["anti_spoofing"] is False and applied["policy"]["liveness_steps"] == 3
    assert applied["policy"]["updated_by"] == ADMIN_EMAIL  # lo cambió quien lo pidió...
    assert applied["change"]["decided_by"] == SECOND_ADMIN_EMAIL  # ...y lo aprobó otro
    again = client.post(approve, headers=other)
    assert again.status_code == 409 and again.json()["code"] == "POLICY_CHANGE_NOT_PENDING"
    missing = client.post(f"{url}/changes/999/approve", headers=other)
    assert missing.status_code == 404 and missing.json()["code"] == "POLICY_CHANGE_NOT_FOUND"


def test_a_pending_change_can_be_rejected_or_cancelled(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    first = put(client, url, admin, qr_lifetime_seconds=120)["data"]["change"]
    second = put(client, url, admin, lockout_enabled=False)["data"]["change"]
    other = second_admin(client)

    no_note = client.post(f"{url}/changes/{first['id']}/reject", json={"note": " "}, headers=other)
    assert no_note.status_code == 422
    own = client.post(f"{url}/changes/{first['id']}/reject", json={"note": "Mejor no"}, headers=admin)
    assert own.status_code == 409 and own.json()["code"] == "POLICY_SELF_DECISION"
    rejected = client.post(f"{url}/changes/{first['id']}/reject", json={"note": "Sin justificar"}, headers=other)
    assert rejected.status_code == 200 and rejected.json()["data"]["status"] == "REJECTED"
    assert rejected.json()["data"]["decision_note"] == "Sin justificar"

    admin = admin_policy(client, company_headers)[1]  # la sesión del primer ADMIN
    not_mine = client.post(f"{url}/changes/{second['id']}/cancel", headers=other)
    assert not_mine.status_code == 409 and not_mine.json()["code"] == "POLICY_NOT_REQUESTER"
    cancelled = client.post(f"{url}/changes/{second['id']}/cancel", headers=admin)
    assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "CANCELLED"
    assert [c["status"] for c in history(client, url, admin, "?status=CANCELLED")] == ["CANCELLED"]
    assert len(history(client, url, admin)) == 2
    assert client.get(url, headers=admin).json()["data"]["qr_lifetime_seconds"] == 30


def test_an_old_or_stale_pending_change_is_not_applied(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    stale = put(client, url, admin, liveness_steps=1)["data"]["change"]
    put(client, url, admin, liveness_steps=3)  # endurece el mismo campo después de pedirlo
    other = second_admin(client)
    changed = client.post(f"{url}/changes/{stale['id']}/approve", headers=other)
    assert changed.status_code == 409 and changed.json()["code"] == "POLICY_CHANGED_SINCE"

    admin = admin_policy(client, company_headers)[1]
    old = put(client, url, admin, max_travel_kmh=400)["data"]["change"]
    with SessionLocal() as db:
        moment = datetime.now(UTC) - timedelta(hours=settings.POLICY_CHANGE_APPROVAL_HOURS + 1)
        db.execute(update(PolicyChange).where(PolicyChange.id == old["id"]).values(created_at=moment))
        db.commit()
    expired = client.post(f"{url}/changes/{old['id']}/approve", headers=second_admin(client))
    assert expired.status_code == 409 and expired.json()["code"] == "POLICY_CHANGE_EXPIRED"
    statuses = {c["id"]: (c["status"], c["decision_note"]) for c in history(client, url, admin)}
    assert statuses[stale["id"]][0] == statuses[old["id"]][0] == "CANCELLED"
    assert statuses[old["id"]][1] == "Venció sin aprobarse"


def test_without_the_two_person_rule_relaxing_applies_now(client, company_headers, monkeypatch):
    monkeypatch.setattr(settings, "POLICY_TWO_PERSON_RULE", False)
    url, admin = admin_policy(client, company_headers)
    body = put(client, url, admin, block_virtual_cameras=False)
    assert body["data"]["change"]["status"] == "APPLIED" and body["data"]["change"]["relaxes"] is True
    assert (
        body["data"]["policy"]["block_virtual_cameras"] is False and body["data"]["policy"]["two_person_rule"] is False
    )


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"duplicate_confidence": 0.5}, "INVALID_CONFIDENCE_LEVEL"),
        ({"employee_device_mode": "NUNCA"}, "INVALID_DEVICE_MODE"),
        ({"risk_high_action": "CASTIGAR"}, "INVALID_RISK_ACTION"),
        ({"risk_fallback_action": "DENY"}, "INVALID_RISK_FALLBACK"),
        ({"risk_medium_score": 70}, "RISK_SCORES_ORDER"),
        ({"risk_signals": {"INVENTADA": {"mode": "ENFORCE"}}}, "INVALID_RISK_SIGNAL"),
        ({"risk_signals": {"FLASH_FLAT": {"mode": "A_VECES"}}}, "INVALID_SIGNAL_MODE"),
        ({"voice_profile": "ROBOTICA"}, "INVALID_VOICE_PROFILE"),
    ],
)
def test_the_policy_only_accepts_catalog_codes_and_ordered_scores(client, company_headers, changes, code):
    url, admin = admin_policy(client, company_headers)
    response = client.put(url, json=changes, headers=admin)
    assert response.status_code == 422 and response.json()["code"] == code


def test_the_voice_guidance_is_configured_and_applies_at_once(client, company_headers):
    """Guía por audio (decisión del dueño, 2026-10-08): apagada por omisión con la voz `FEMALE_WARM`; encenderla y
    cambiar la voz son neutrales (no relajan), así que aplican al momento aunque la regla de dos personas esté activa,
    y la empresa las lee en su política."""
    url, admin = admin_policy(client, company_headers)
    policy = client.get(url, headers=admin).json()["data"]
    assert policy["voice_guidance_enabled"] is False and policy["voice_profile"] == "FEMALE_WARM"
    company = client.get(SETTINGS_URL, headers=company_headers).json()["data"]
    assert company["voice_guidance_enabled"] is False and company["voice_profile"] == "FEMALE_WARM"
    body = put(client, url, admin, voice_guidance_enabled=True, voice_profile="MALE_DEEP")
    assert body["code"] == "POLICY_UPDATED" and body["data"]["change"]["relaxes"] is False
    new_policy = body["data"]["policy"]
    assert new_policy["voice_guidance_enabled"] is True and new_policy["voice_profile"] == "MALE_DEEP"
    updated = client.get(SETTINGS_URL, headers=company_headers).json()["data"]
    assert updated["voice_guidance_enabled"] is True and updated["voice_profile"] == "MALE_DEEP"


def test_each_risk_signal_is_adjusted_and_relaxing_it_needs_approval(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    tightened = put(client, url, admin, risk_signals={"FLASH_FLAT": {"mode": "ENFORCE", "points": 40}})["data"]
    flat = next(s for s in tightened["policy"]["risk_signals"] if s["code"] == "FLASH_FLAT")
    assert (flat["mode"], flat["points"]) == ("ENFORCE", 40)
    assert {c["field"] for c in tightened["change"]["changes"]} == {
        "risk_signals.FLASH_FLAT.mode",
        "risk_signals.FLASH_FLAT.points",
    }
    assert tightened["change"]["simulation"] is not None
    relaxed = put(client, url, admin, risk_signals={"FLASH_FLAT": {"mode": "OBSERVE"}})["data"]["change"]
    assert relaxed["status"] == "PENDING" and relaxed["changes"] == [
        {"field": "risk_signals.FLASH_FLAT.mode", "before": "ENFORCE", "after": "OBSERVE", "relaxes": True}
    ]
    applied = client.post(f"{url}/changes/{relaxed['id']}/approve", headers=second_admin(client)).json()["data"]
    flat = next(s for s in applied["policy"]["risk_signals"] if s["code"] == "FLASH_FLAT")
    assert (flat["mode"], flat["points"]) == ("OBSERVE", 40)
    # Volver a los valores de la plataforma no deja ajustes guardados.
    set_policy(client, company_headers, risk_signals={"FLASH_FLAT": {"points": 30}})
    with SessionLocal() as db:
        assert db.query(VerificationPolicy).one().risk_signals == {}


def test_presets_go_through_the_same_path(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    high = client.post(f"{url}/preset", json={"preset": "HIGH", "reason": "Fraude confirmado"}, headers=admin)
    assert high.status_code == 200 and high.json()["code"] == "POLICY_UPDATED"
    policy = high.json()["data"]["policy"]
    # El destello se retiró (2026-10-06): ningún nivel lo enciende.
    assert policy["preset"] == "HIGH" and policy["liveness_steps"] == 3 and policy["flash_liveness"] == "OFF"
    replay = next(s for s in policy["risk_signals"] if s["code"] == "REPLAY_PERCEPTUAL")
    assert replay["mode"] == "ENFORCE"
    assert high.json()["data"]["change"]["preset"] == "HIGH"
    again = client.post(f"{url}/preset", json={"preset": "HIGH"}, headers=admin)
    assert again.json()["code"] == "POLICY_UNCHANGED"
    # Volver a Estándar relaja: espera a otro ADMIN.
    standard = client.post(f"{url}/preset", json={"preset": "STANDARD"}, headers=admin)
    assert standard.json()["code"] == "POLICY_CHANGE_PENDING" and standard.json()["data"]["policy"]["preset"] == "HIGH"
    approved = client.post(
        f"{url}/changes/{standard.json()['data']['change']['id']}/approve", headers=second_admin(client)
    ).json()["data"]["policy"]
    assert approved["preset"] == "STANDARD" and approved["liveness_steps"] == 2
    assert all(s["mode"] == s["default_mode"] for s in approved["risk_signals"])
    invalid = client.post(f"{url}/preset", json={"preset": "EXTREMO"}, headers=admin_policy(client, company_headers)[1])
    assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_POLICY_PRESET"
    # Un ajuste a mano después de un nivel deja la política "a la medida".
    custom = put(client, url, admin_policy(client, company_headers)[1], liveness_steps=3)["data"]["policy"]
    assert custom["preset"] is None


PRESENCE = ("validator_signing", "validator_location", "site_codes")
#: Las señales del protocolo de captura que Máximo exige (sin destello dictado o fuera de tiempo, alterado, sin ráfaga).
#: Antifraude 2a tras retirar el destello (2026-10-06): el nivel Máximo exige solo la ráfaga.
PROTOCOL = ("BURST_MISSING",)
#: Las señales del destello ya no las exige ningún nivel (sin destello nunca se miden).
FLASH_SIGNALS = ("FLASH_UNPACED", "FLASH_PACE_TIMING", "FLASH_PACE_MISMATCH", "FLASH_FLAT")


def test_maximum_requires_the_capture_protocol_and_the_presence_proofs(client, company_headers):
    """Decisión del dueño (2026-10-06): Máximo exige el protocolo de captura (destello dictado y ráfaga) y las pruebas
    de presencia (firma y ubicación de los validadores y código de sitio); Alto y Estándar las dejan en «Solo medir».
    Bajar de Máximo relaja: pasa por la regla de dos personas como cualquier cambio."""
    url, admin = admin_policy(client, company_headers)
    policy = client.get(url, headers=admin).json()["data"]
    assert [policy[field] for field in PRESENCE] == ["OBSERVE"] * 3  # nacen midiendo
    maximum = client.post(f"{url}/preset", json={"preset": "MAXIMUM"}, headers=admin).json()
    assert maximum["code"] == "POLICY_UPDATED"  # endurece: aplica al momento
    applied = maximum["data"]["policy"]
    assert applied["flash_paced"] is False and applied["capture_burst"] is True  # el destello, retirado; la ráfaga, sí
    assert [applied[field] for field in PRESENCE] == ["ENFORCE"] * 3
    signals = {s["code"]: s["mode"] for s in applied["risk_signals"]}
    assert [signals[code] for code in PROTOCOL] == ["ENFORCE"]
    assert all(signals[code] == "OBSERVE" for code in FLASH_SIGNALS)  # nunca se exigen: sin destello no se miden
    assert signals["BURST_FROZEN"] == "OBSERVE"  # lo que detecta la ráfaga sigue calibrándose
    # 2026-10-08 (endurecimiento): Máximo activa el bloqueo de lentes, exige la ubicación de cada verificación, sube los
    # umbrales a su extremo estricto y agrega la firma del validador como regla dura.
    assert applied["block_glasses"] is True and applied["verification_location"] == "ENFORCE"
    assert applied["min_capture_quality"] == 0.9 and applied["liveness_timeout_seconds"] == 20
    assert (applied["lockout_max_failures"], applied["lockout_minutes"]) == (3, 1440)
    assert applied["qr_lifetime_seconds"] == 15 and signals["VALIDATOR_SIGNATURE_INVALID"] == "ENFORCE"
    high = client.post(f"{url}/preset", json={"preset": "HIGH"}, headers=admin).json()
    assert high["code"] == "POLICY_CHANGE_PENDING" and high["data"]["policy"]["validator_signing"] == "ENFORCE"
    relaxed = {change["field"] for change in high["data"]["change"]["changes"] if change["relaxes"]}
    assert set(PRESENCE) <= relaxed and {f"risk_signals.{code}.mode" for code in PROTOCOL} <= relaxed
    approved = client.post(
        f"{url}/changes/{high['data']['change']['id']}/approve", headers=second_admin(client)
    ).json()["data"]["policy"]
    assert approved["preset"] == "HIGH" and [approved[field] for field in PRESENCE] == ["OBSERVE"] * 3
    assert approved["flash_paced"] is False and approved["capture_burst"] is True  # la ráfaga sigue midiendo
    assert all(s["mode"] == "OBSERVE" for s in approved["risk_signals"] if s["code"] in PROTOCOL)


def test_standard_and_high_keep_the_new_proofs_measuring(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    put(client, url, admin, validator_location="ENFORCE")  # un ajuste a mano que endurece
    pending = client.post(f"{url}/preset", json={"preset": "STANDARD"}, headers=admin).json()
    assert pending["code"] == "POLICY_CHANGE_PENDING"  # el nivel lo bajaría a «Solo medir»: dos personas
    changes = {change["field"]: change for change in pending["data"]["change"]["changes"]}
    assert (changes["validator_location"]["before"], changes["validator_location"]["after"]) == ("ENFORCE", "OBSERVE")


def _assessment(company_id: int, hits: list[Hit], *, label: str | None = None, step_up: bool = False) -> RiskAssessment:
    config = config_for(PolicySnapshot())
    decision = decide(hits, config)
    return RiskAssessment(
        company_id=company_id,
        score=decision.score,
        tier=decision.tier,
        action=decision.action,
        reasons=[r.as_dict() for r in decision.reasons],
        policy_version=config.version(),
        engine="1.0.0",
        step_up=step_up,
        fraud_label=label,
    )


def test_the_simulation_replays_the_last_days_with_the_candidate(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    company_id = company_id_of(client, company_headers)
    with SessionLocal() as db:
        db.add_all(
            [
                _assessment(company_id, [Hit("FLASH_FLAT", 1.0, 1.25)], label="FRAUD"),
                _assessment(company_id, [Hit("FLASH_FLAT", 1.05, 1.25)], label="GENUINE"),
                _assessment(company_id, [Hit("CAMERA_LABEL_MISSING")]),
                _assessment(company_id, [Hit("SPOOF_PROB_LOW", 0.08, 0.1)], step_up=True),
                _assessment(company_id, []),
            ]
        )
        db.commit()
    base = client.post(f"{url}/simulate", json={}, headers=admin).json()["data"]
    assert base["evaluated"] == 5 and base["capped"] is False and base["stricter"] == base["looser"] == 0
    assert base["current"]["allow"] == 5 and base["frauds"] == 1 and base["frauds_stopped"] == 0

    candidate = {
        "risk_signals": {"FLASH_FLAT": {"mode": "ENFORCE"}, "CAMERA_LABEL_MISSING": {"mode": "ENFORCE", "points": 30}}
    }
    result = client.post(f"{url}/simulate", json=candidate, headers=admin)
    assert result.status_code == 200 and result.json()["code"] == "POLICY_SIMULATED"
    data = result.json()["data"]
    assert data["candidate"]["step_up"] == 3 and data["candidate"]["allow"] == 2 and data["stricter"] == 3
    assert data["frauds_stopped"] == 1 and data["genuine_affected"] == 2
    assert data["top_reasons"] == [{"code": "FLASH_FLAT", "count": 2}, {"code": "CAMERA_LABEL_MISSING", "count": 1}]
    looser = client.post(f"{url}/simulate", json={"risk_engine": False}, headers=admin).json()["data"]
    assert looser["looser"] == 0 and looser["candidate"]["allow"] == 5
    bad = client.post(f"{url}/simulate", json={"risk_high_score": 20}, headers=admin)
    assert bad.status_code == 422 and bad.json()["code"] == "RISK_SCORES_ORDER"


def test_a_company_without_its_policy_row_gets_safe_defaults(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    with SessionLocal() as db:
        db.execute(delete(VerificationPolicy))
        db.commit()
    assert client.get(url, headers=admin).json()["data"]["risk_engine"] is True
    with SessionLocal() as db:
        db.execute(delete(VerificationPolicy))
        db.commit()
    assert put(client, url, admin, liveness_steps=3)["data"]["policy"]["liveness_steps"] == 3
