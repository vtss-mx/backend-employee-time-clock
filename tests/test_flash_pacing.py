"""Destello dictado por el servidor (antifraude fase 2a): el token sellado de cada paso, el canal en vivo que revela los
colores uno por uno, el respaldo sin canal y la verificación del comprobante con las capturas (a tiempo, a destiempo,
alterado o sin dictar)."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.facial_recognition.photometry import FLASH_PALETTE, flash_hex
from app.models import FaceAttemptMetric
from app.services import flash_pacing
from app.services.face_signals import PaceVerdict
from app.services.flash_pacing import FlashTokenInvalid, PaceState
from app.services.liveness_service import Challenge
from tests.conftest import FLASH_CODES, approved_employee, flash_files, turn_files
from tests.test_realtime import connect
from tests.test_risk_engine import VERIFY, last_reasons

NOW = 1_800_000_000_000


def challenge(colors=("RED", "GREEN", "BLUE"), user_id=7, minutes=1) -> Challenge:
    issued = datetime.fromtimestamp(NOW / 1000, UTC)
    return Challenge(
        id="reto-1",
        user_id=user_id,
        actions=(),
        issued_at=issued,
        expires_at=issued + timedelta(minutes=minutes),
        flash=tuple(colors),
        flash_paced=True,
    )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(token: str, frames: list[bytes], *, user_id=7, every=400) -> tuple[list[str], str]:
    """Recorre la secuencia como la app: los colores revelados y el comprobante final."""
    now = NOW
    step = flash_pacing.advance(token, user_id, None, now)
    colors = []
    for frame in frames:
        assert not step.done and step.color is not None and step.total == len(frames)
        colors.append(step.color)
        now += every
        step = flash_pacing.advance(step.token, user_id, digest(frame), now)
    assert step.done and step.step == len(frames)
    return colors, step.token


# ---------------------------------------------------------------- token sellado


def test_the_sequence_reveals_fresh_colors_one_by_one_and_hands_a_receipt():
    frames = [b"uno", b"dos", b"tres"]
    token = flash_pacing.start_token(challenge())
    colors, receipt = run(token, frames)
    assert all(c in {flash_hex(code) for code in FLASH_PALETTE} for c in colors)
    assert all(a != b for a, b in pairwise(colors))  # nunca el mismo dos veces seguidas
    outcome = flash_pacing.verify(receipt, challenge(), 7, frames)
    assert outcome.verdict == PaceVerdict.PACED and outcome.slowest_ms == 400
    assert [flash_hex(code) for code in outcome.colors] == colors
    # Reiniciar desde el token inicial da colores nuevos (se eligen al revelarse): 6 colores, 30 vueltas.
    firsts = {flash_pacing.advance(token, 7, None, NOW).color for _ in range(30)}
    assert len(firsts) > 1


@pytest.mark.parametrize(
    ("token_of", "user", "now", "digest_value"),
    [
        (lambda t: t[:-4] + "AAAA", 7, NOW, None),  # alterado
        (lambda t: t, 8, NOW, None),  # de otra cuenta
        (lambda t: t, 7, NOW + 61_000, None),  # el reto ya venció
        (lambda t: t, 7, NOW, "a" * 64),  # una huella sin color revelado
        (lambda t: "basura", 7, NOW, None),
    ],
)
def test_an_unusable_token_is_refused(token_of, user, now, digest_value):
    token = flash_pacing.start_token(challenge())
    with pytest.raises(FlashTokenInvalid) as refused:
        flash_pacing.advance(token_of(token), user, digest_value, now)
    assert refused.value.code == "FLASH_TOKEN_INVALID" and refused.value.status_code == 422


def test_a_revealed_color_needs_a_well_formed_digest_and_a_finished_sequence_ends():
    token = flash_pacing.start_token(challenge(colors=("RED",)))
    step = flash_pacing.advance(token, 7, None, NOW)
    for bad in (None, "XYZ", "A" * 64):
        with pytest.raises(FlashTokenInvalid):
            flash_pacing.advance(step.token, 7, bad, NOW + 300)
    done = flash_pacing.advance(step.token, 7, "b" * 64, NOW + 300)
    assert done.done
    with pytest.raises(FlashTokenInvalid):
        flash_pacing.advance(done.token, 7, None, NOW + 400)
    # Un reto sin colores no se puede dictar.
    with pytest.raises(FlashTokenInvalid):
        flash_pacing.advance(flash_pacing.start_token(challenge(colors=())), 7, None, NOW)
    # Un token con colores revelados pero sin uno en curso (estado imposible de la app) tampoco empieza otra vez.
    odd = flash_pacing.seal(PaceState("reto-1", 7, 2, NOW // 1000 + 60, colors=("RED",)))
    with pytest.raises(FlashTokenInvalid):
        flash_pacing.advance(odd, 7, None, NOW)


def test_tokens_survive_a_key_rotation_and_reject_malformed_payloads(monkeypatch):
    token = flash_pacing.start_token(challenge())
    rotated = MultiFernet([Fernet(Fernet.generate_key()), *flash_pacing._SEAL._fernets])
    monkeypatch.setattr(flash_pacing, "_SEAL", rotated)
    assert flash_pacing.advance(token, 7, None, NOW).color is not None  # la llave anterior aún abre
    for payload in ({"c": "x"}, [], {"c": "x", "u": 7, "n": 1, "x": 1, "k": [1], "d": [], "l": [], "r": None, "f": []}):
        sealed = rotated.encrypt(json.dumps(payload).encode()).decode()
        assert flash_pacing.unseal(sealed, 7, None) is None


def test_the_fallback_gives_the_static_colors_only_from_the_initial_token():
    token = flash_pacing.start_token(challenge())
    assert flash_pacing.fallback_colors(token, 7, NOW) == ["#FF0000", "#00FF00", "#0000FF"]
    started = flash_pacing.advance(token, 7, None, NOW).token
    for bad in (started, "basura"):
        with pytest.raises(FlashTokenInvalid):
            flash_pacing.fallback_colors(bad, 7, NOW)


# ---------------------------------------------------------------- comprobante


def test_the_receipt_is_checked_against_the_uploaded_captures():
    frames = [b"uno", b"dos", b"tres"]
    token = flash_pacing.start_token(challenge())
    _, receipt = run(token, frames)
    assert flash_pacing.verify(None, challenge(), 7, frames).verdict == PaceVerdict.UNPACED
    tampered = flash_pacing.verify(receipt, challenge(), 7, [b"uno", b"otra", b"tres"])
    assert (tampered.verdict, tampered.value) == (PaceVerdict.MISMATCH, 1.0)
    for outcome in (
        flash_pacing.verify(receipt, replace(challenge(), id="otro"), 7, frames),  # de otro reto
        flash_pacing.verify(receipt, challenge(), 8, frames),  # de otra cuenta
        flash_pacing.verify(receipt, challenge(), 7, frames[:2]),  # faltan capturas
        flash_pacing.verify(flash_pacing.advance(token, 7, None, NOW).token, challenge(), 7, frames),  # incompleto
        flash_pacing.verify("-", challenge(), 7, frames),
    ):
        assert (outcome.verdict, outcome.value, outcome.colors) == (PaceVerdict.MISMATCH, -1.0, ())


@pytest.mark.parametrize(("every", "value", "threshold"), [(2500, 2500.0, 2000.0), (100, 100.0, 250.0)])
def test_a_color_answered_outside_its_window_is_out_of_time(every, value, threshold):
    frames = [b"uno", b"dos"]
    colors, receipt = run(flash_pacing.start_token(challenge(colors=("RED", "BLUE"))), frames, every=every)
    outcome = flash_pacing.verify(receipt, challenge(colors=("RED", "BLUE")), 7, frames)
    assert (outcome.verdict, outcome.value, outcome.threshold) == (PaceVerdict.TIMING, value, threshold)
    assert [flash_hex(code) for code in outcome.colors] == colors  # los colores que sí se pintaron


# ---------------------------------------------------------------- canal en vivo y respaldo HTTP


def employee_token(client, company_headers) -> str:
    return approved_employee(client, company_headers)["Authorization"].removeprefix("Bearer ")


def test_the_live_channel_dictates_each_color_and_hands_the_receipt(client, company_headers):
    access = employee_token(client, company_headers)
    headers = {"Authorization": f"Bearer {access}"}
    pace = client.post("/api/face/challenge", headers=headers).json()["data"]["flash_pace"]
    ws, socket = connect(client, access)
    try:
        socket.send_json({"type": "flash", "id": "flash-0001", "token": pace["token"]})
        step = socket.receive_json()
        assert step["code"] == "FLASH_COLOR" and step["traceId"] == "flash-0001"
        assert step["data"]["step"] == 0 and step["data"]["total"] == pace["total"]
        assert step["data"]["window_ms"] == settings.FACE_FLASH_PACE_WINDOW_MS and step["data"]["color"] in FLASH_CODES
        for _ in range(pace["total"]):
            socket.send_json({"type": "flash", "token": step["data"]["token"], "digest": "c" * 64})
            step = socket.receive_json()
        assert step["code"] == "FLASH_DONE" and step["data"]["receipt"]
        socket.send_json({"type": "flash", "token": "basura"})
        assert socket.receive_json()["code"] == "FLASH_TOKEN_INVALID"
        socket.send_json({"type": "flash", "token": 7})
        assert socket.receive_json()["code"] == "BAD_MESSAGE"
        socket.send_json({"type": "flash", "token": pace["token"], "digest": 7})
        assert socket.receive_json()["code"] == "BAD_MESSAGE"
    finally:
        ws.__exit__(None, None, None)


def test_only_who_captures_faces_follows_the_flash(client, admin_headers):
    """El ADMIN valida formularios en el canal, pero no captura rostros: no sigue el destello."""
    ws, socket = connect(client, admin_headers["Authorization"].removeprefix("Bearer "))
    try:
        socket.send_json({"type": "flash", "token": "x"})
        denied = socket.receive_json()
        assert denied["statusCode"] == 403 and denied["code"] == "FORBIDDEN"
    finally:
        ws.__exit__(None, None, None)


def test_without_the_channel_the_app_asks_for_the_static_colors(client, company_headers):
    headers = approved_employee(client, company_headers)
    pace = client.post("/api/face/challenge", headers=headers).json()["data"]["flash_pace"]
    colors = client.post("/api/face/challenge/flash", json={"token": pace["token"]}, headers=headers)
    assert colors.status_code == 200 and colors.json()["code"] == "FLASH_COLORS"
    assert len(colors.json()["data"]["flash"]) == pace["total"]
    refused = client.post("/api/face/challenge/flash", json={"token": "basura"}, headers=headers)
    assert refused.status_code == 422 and refused.json()["code"] == "FLASH_TOKEN_INVALID"


# ---------------------------------------------------------------- en el intento


def attempt(client, headers, *, paced=True, mutate=None):
    challenge_data = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(2)]
    flash = flash_files(challenge_data, "juan", paced=paced)
    files += turn_files(challenge_data, "juan") + (mutate(flash) if mutate else flash)
    data = {"challenge_id": challenge_data["challenge_id"], "camera_label": "Cam"}
    return client.post(VERIFY, data=data, files=files, headers=headers)


def last_metric() -> FaceAttemptMetric | None:
    with SessionLocal() as db:
        return db.scalars(select(FaceAttemptMetric).order_by(FaceAttemptMetric.id.desc())).first()


def test_a_paced_flash_is_measured_with_the_colors_that_were_really_shown(client, company_headers):
    headers = approved_employee(client, company_headers)
    assert attempt(client, headers).json()["data"]["verified"] is True
    assert not {"FLASH_UNPACED", "FLASH_PACE_TIMING", "FLASH_PACE_MISMATCH", "FLASH_WEAK"} & set(last_reasons())
    metric = last_metric()
    assert metric is not None and metric.flash_pace_ms == 400
    assert metric.flash_score is not None and metric.flash_score > 0.9


def test_an_unpaced_or_tampered_flash_is_a_measured_signal_never_a_rejection(client, company_headers):
    headers = approved_employee(client, company_headers)
    assert attempt(client, headers, paced=False).json()["data"]["verified"] is True
    metric = last_metric()
    assert last_reasons()["FLASH_UNPACED"]["mode"] == "OBSERVE" and metric is not None and metric.flash_pace_ms is None

    def swap(parts: list) -> list:
        """Otra captura en lugar de la comprometida (mismo color, otros bytes)."""
        name, (filename, content, kind) = parts[0]
        return [(name, (filename, content + b"#otra", kind)), *parts[1:]]

    assert attempt(client, headers, mutate=swap).json()["data"]["verified"] is True
    assert last_reasons()["FLASH_PACE_MISMATCH"]["value"] == 1.0

    def forge(parts: list) -> list:
        return [*parts[:-1], ("flash_receipt", (None, "x" * (settings.WS_MAX_MESSAGE_BYTES + 1)))]

    assert attempt(client, headers, mutate=forge).json()["data"]["verified"] is True
    assert last_reasons()["FLASH_PACE_MISMATCH"]["value"] == -1.0


def test_a_slow_paced_flash_is_out_of_time(client, company_headers, monkeypatch):
    headers = approved_employee(client, company_headers)
    monkeypatch.setattr(settings, "FACE_FLASH_PACE_WINDOW_MS", 300)
    assert attempt(client, headers).json()["data"]["verified"] is True
    assert last_reasons()["FLASH_PACE_TIMING"] == {**last_reasons()["FLASH_PACE_TIMING"], "value": 400.0}


def test_without_the_policy_the_flash_travels_in_the_challenge_as_before(client, company_headers):
    from tests.test_policy import set_policy

    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, flash_paced=False)
    data = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert data["flash_pace"] is None and len(data["flash"]) == settings.FACE_FLASH_COLORS
    assert attempt(client, headers).json()["data"]["verified"] is True
    assert "FLASH_UNPACED" not in last_reasons()
