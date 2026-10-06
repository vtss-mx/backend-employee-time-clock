"""Protocolo de captura de frontera (antifraude fase 2a): la ráfaga de recortes (validación estricta, análisis y sus
señales), las señales físicas de las capturas (moiré, ruido, paralaje), el pulso que solo se mide, la política del ADMIN
(interruptores, niveles y lo que no se puede exigir) y lo que ve Seguridad facial."""

import json
import logging
import threading
import time
from datetime import UTC, datetime

import cv2
import pytest
from sqlalchemy import select

import app.facial_recognition as face_module
from app.core.config import settings
from app.core.database import SessionLocal
from app.facial_recognition.burst import BurstLayout, Pulse
from app.facial_recognition.pipeline import BurstAnalysis
from app.facial_recognition.worker_pool import WorkerPool
from app.models import FaceAttemptMetric, FraudEvidence
from app.services import capture_protocol, face_security
from app.services.capture_protocol import burst_layout, measure_burst, parallax_of, protocol_hits
from app.services.face_signals import AttemptSignals, BurstOutcome, PaceOutcome, PaceVerdict
from app.services.liveness_service import LivenessResponse
from app.services.risk_rules import MEASURE_ONLY_SIGNALS, Hit, RiskConfig, SignalSetting, decide
from tests.conftest import (
    FRONTAL_POINTS,
    FakePipeline,
    _analysis,
    _step_points,
    approved_employee,
    burst_files,
    flash_files,
    turn_files,
)
from tests.test_liveness_v2 import ADMIN_URL, add_metrics
from tests.test_policy import admin_policy, set_policy
from tests.test_risk_engine import VERIFY, last_reasons

HOLDS, MOVES = settings.FACE_BURST_HOLD_FRAMES, settings.FACE_BURST_MOVE_FRAMES


def meta(**changes) -> str:
    description = {
        "v": 1,
        "tile": settings.FACE_BURST_TILE_PX,
        "cols": 8,
        "t": [100 * i for i in range(HOLDS)] + [5000 + 100 * i for i in range(MOVES)],
        "s": "H" * HOLDS + "M" * MOVES,
    }
    return json.dumps({**description, **changes})


# ---------------------------------------------------------------- descripción de la hoja


def test_a_well_described_burst_is_laid_out_as_the_server_asked():
    layout = burst_layout(meta())
    assert layout is not None and layout.count == HOLDS + MOVES and layout.tile == settings.FACE_BURST_TILE_PX
    assert layout.indices("H") == list(range(HOLDS)) and layout.size == (8 * layout.tile, 5 * layout.tile)
    only_hold = burst_layout(meta(t=[100 * i for i in range(HOLDS)], s="H" * HOLDS))
    assert only_hold is not None and only_hold.indices("M") == []


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "no es json",
        "x" * (capture_protocol.max_meta_chars() + 1),
        meta(v=2),
        meta(extra=1),
        meta(tile=128),  # no es el lado que se pidió
        meta(t=[0, 100, 200], s="HHH"),  # muy pocos recortes
        meta(s="H" * (HOLDS + MOVES - 1) + "H"),  # más quietos de los pedidos
        meta(t=[100 * i for i in range(MOVES + 1)], s="M" * (MOVES + 1)),  # más de movimiento
        meta(s="M" + "H" * (HOLDS + MOVES - 1)),  # tramos fuera de orden
        meta(cols=64),  # más columnas que recortes
        meta(t=[-1] + [100 * i for i in range(1, HOLDS)] + [5000 + 100 * i for i in range(MOVES)]),
        meta(t=[0, 10] + [100 * i for i in range(2, HOLDS)] + [5000 + 100 * i for i in range(MOVES)]),  # muy juntos
        meta(t=[400 * i for i in range(HOLDS)] + [20_000 + 100 * i for i in range(MOVES)]),  # tramo muy largo
        meta(t=[100 * i for i in range(HOLDS)] + [100 * i for i in range(MOVES)]),  # el movimiento antes del final
        meta(t=[100 * i for i in range(HOLDS + MOVES - 1)]),  # tiempos y tramos de distinto largo
    ],
)
def test_anything_off_in_the_description_is_a_malformed_burst(raw):
    assert burst_layout(raw) is None


# ---------------------------------------------------------------- medir la ráfaga


def response(burst: bytes | None = b"burst:juan", raw: str | None = None, oversize: bool = False) -> LivenessResponse:
    return LivenessResponse(burst=burst, burst_meta=meta() if raw is None else raw, burst_oversize=oversize)


def test_measuring_the_burst_never_breaks_the_attempt(caplog):
    frontal = [_analysis("juan")]
    pipeline = FakePipeline()
    assert measure_burst(pipeline, response(None), frontal) == BurstOutcome()  # no llegó
    assert measure_burst(pipeline, response(None, oversize=True), frontal) == BurstOutcome(malformed=True)
    assert measure_burst(pipeline, response(raw="{}"), frontal) == BurstOutcome(malformed=True)
    assert measure_burst(pipeline, response(b"burst-broken:juan"), frontal) == BurstOutcome(malformed=True)
    measured = measure_burst(pipeline, response(), frontal)
    assert measured is not None and measured.analysis is not None and measured.anchor == pytest.approx(1.0)
    alone = measure_burst(pipeline, response(), [])  # sin frontales no hay con qué comparar el ancla
    assert alone is not None and alone.analysis is not None and alone.anchor is None
    other = measure_burst(pipeline, response(b"burst:pedro"), frontal)
    assert other is not None and other.anchor is not None and other.anchor < 0.3  # otra persona en la ráfaga

    class Broken(FakePipeline):
        def __init__(self, error: Exception) -> None:
            self.error = error

        def analyze_burst(self, data, layout, rules):
            raise self.error

    assert measure_burst(Broken(cv2.error("x")), response(), frontal) == BurstOutcome(malformed=True)
    assert measure_burst(Broken(ValueError("x")), response(), frontal) == BurstOutcome(malformed=True)
    with caplog.at_level(logging.ERROR):
        assert measure_burst(Broken(RuntimeError("x")), response(), frontal) is None  # el motor: sin medir
    assert "ráfaga" in caplog.text


@pytest.fixture
def spare_workers(monkeypatch):
    """Un pool "cargado" con dos workers libres (como un proceso con dos núcleos sin carga): la ráfaga se mide en uno
    de repuesto, en paralelo (decisión del dueño, 2026-10-06)."""
    holder = face_module._PipelineHolder()
    holder._pool = WorkerPool(lambda _: FakePipeline(), size=2, max_waiting=2, wait_timeout=1)
    monkeypatch.setattr(face_module, "_holder", holder)
    yield holder._pool
    holder.spare_threads(2).shutdown(wait=True)


def _idle(pool: WorkerPool) -> None:
    deadline = time.monotonic() + 5
    while pool.stats().busy:
        assert time.monotonic() < deadline, "el worker de repuesto no regresó"
        time.sleep(0.005)


def test_the_burst_is_measured_in_parallel_on_a_spare_worker(spare_workers):
    frontal = [_analysis("juan")]
    mine = FakePipeline()
    pending = capture_protocol.BurstMeasure(mine, response(), frontal)
    parallel, alone = pending.result(), measure_burst(mine, response(), frontal)
    assert parallel is not None and alone is not None and parallel.analysis is not None
    assert (parallel.analysis.frames, parallel.anchor) == (alone.analysis.frames, alone.anchor)  # lo mismo que antes
    _idle(spare_workers)
    assert spare_workers.stats().processed == 1  # lo midió el repuesto
    # Sin hoja que analizar no ocupa otro núcleo: se resuelve al recogerla, con el worker de la petición.
    assert capture_protocol.BurstMeasure(mine, response(None), frontal).result() == BurstOutcome()
    oversize = capture_protocol.BurstMeasure(mine, response(None, oversize=True), frontal).result()
    assert oversize == BurstOutcome(malformed=True) and spare_workers.stats().processed == 1


def test_without_a_free_worker_the_burst_is_measured_at_the_end_as_before(spare_workers):
    taken = [spare_workers.try_acquire(), spare_workers.try_acquire()]  # los dos ocupados (carga)
    outcome = capture_protocol.BurstMeasure(FakePipeline(), response(), [_analysis("juan")]).result()
    assert outcome is not None and outcome.analysis is not None and spare_workers.stats().processed == 0
    for worker in taken:
        spare_workers.release(worker)


def test_a_parallel_burst_that_does_not_finish_in_time_is_left_unmeasured(spare_workers, monkeypatch, caplog):
    """Tope `FACE_BURST_WAIT_SECONDS`: el intento sigue sin medirla (registrado) y el repuesto regresa al terminar."""
    release = threading.Event()

    class Slow(FakePipeline):
        def analyze_burst(self, data, layout, rules):
            release.wait(5)
            return super().analyze_burst(data, layout, rules)

    spare_workers._idle = [Slow(), Slow()]
    monkeypatch.setattr(settings, "FACE_BURST_WAIT_SECONDS", 0.05)
    with caplog.at_level(logging.ERROR):
        assert capture_protocol.BurstMeasure(FakePipeline(), response(), [_analysis("juan")]).result() is None
    assert "no terminó" in caplog.text
    release.set()
    _idle(spare_workers)


def test_an_attempt_measures_its_burst_on_a_spare_worker(client, company_headers, spare_workers):
    """Por la API: el resultado y lo guardado son los mismos; si el intento termina antes (un movimiento que no se
    hizo), nadie recoge la ráfaga y su worker regresa solo."""
    headers = approved_employee(client, company_headers)
    _idle(spare_workers)
    before = spare_workers.stats().processed  # el registro también reparte sus fotos entre los repuestos
    answer, _ = verify_with(client, headers)
    assert answer.json()["data"]["verified"] is True
    assert metric().burst_frames == HOLDS + MOVES
    _idle(spare_workers)
    assert spare_workers.stats().processed == before + 1
    failed, _ = verify_with(client, headers, turn="face:{person}")
    assert failed.json()["data"]["verified"] is False and metric().burst_frames is None
    _idle(spare_workers)
    assert spare_workers.stats().processed == before + 2


def test_parallax_is_measured_only_on_turns_and_looks():
    frontal = [_analysis("juan")]
    from dataclasses import replace

    from app.facial_recognition import LivenessAction

    frontal = [replace(frontal[0], landmarks=FRONTAL_POINTS)]
    turn = replace(_analysis("juan"), landmarks=_step_points(LivenessAction.TURN_LEFT, flat=False))
    flat = replace(_analysis("juan"), landmarks=_step_points(LivenessAction.TURN_LEFT, flat=True))
    closer = replace(_analysis("juan"), landmarks=FRONTAL_POINTS * 1.3)
    actions = (LivenessAction.TURN_LEFT, LivenessAction.MOVE_CLOSER)
    assert parallax_of(frontal, [turn, closer], actions) == pytest.approx(0.158, abs=0.01)
    assert parallax_of(frontal, [flat, closer], actions) == pytest.approx(0.0, abs=1e-6)
    assert parallax_of(frontal, [closer], (LivenessAction.MOVE_CLOSER,)) is None
    assert parallax_of([_analysis("juan")], [turn], actions) is None  # frontal sin puntos
    assert parallax_of([], [turn], actions) is None


def limits(**changes) -> face_security.SecurityThresholds:
    base = face_security._thresholds({})
    from dataclasses import replace

    return replace(base, **changes)


def test_each_protocol_signal_fires_from_what_was_measured():
    from dataclasses import replace

    signals = AttemptSignals(frontal=[replace(_analysis("juan"), moire=30.0, noise_ratio=0.1)], parallax=0.01)
    codes = {hit.code for hit in protocol_hits(signals, limits())}
    assert codes == {"MOIRE_HIGH", "NOISE_MISMATCH", "PERSPECTIVE_FLAT"}
    calm = AttemptSignals(frontal=[replace(_analysis("juan"), moire=5.0, noise_ratio=1.0)], parallax=0.2)
    assert protocol_hits(calm, limits()) == []
    assert protocol_hits(AttemptSignals(frontal=[_analysis("juan")]), limits()) == []  # nada medido

    def burst(**values) -> BurstOutcome:
        analysis = BurstAnalysis(frames=40, faceless=0, jumps=0, motion=2.0, repeats=0, pulse=Pulse(1.0, 70.0))
        return BurstOutcome(analysis=replace(analysis, **values), anchor=0.9)

    def hits(outcome: BurstOutcome) -> dict[str, Hit]:
        return {hit.code: hit for hit in protocol_hits(AttemptSignals(burst=outcome), limits())}

    assert hits(burst()) == {}
    assert hits(BurstOutcome())["BURST_MISSING"].value == 0.0
    assert hits(BurstOutcome(malformed=True))["BURST_MISSING"].value == 1.0
    assert hits(burst(jumps=2))["BURST_DISCONTINUOUS"].value == 2.0
    assert "BURST_DISCONTINUOUS" not in hits(burst(faceless=5))  # unos pocos sin rostro se toleran
    assert hits(burst(faceless=30))["BURST_DISCONTINUOUS"].value == 30.0
    assert hits(replace(burst(), anchor=0.1))["BURST_DISCONTINUOUS"].value == 1.0
    assert hits(burst(motion=0.0))["BURST_FROZEN"].threshold == settings.FACE_BURST_MIN_MOTION
    assert "BURST_FROZEN" not in hits(burst(motion=None))
    assert hits(burst(repeats=4))["BURST_LOOP"].value == 4.0
    assert hits(burst(pulse=Pulse(-9.0, 50.0)))["PULSE_ABSENT"].value == -9.0
    assert "PULSE_ABSENT" not in hits(burst(pulse=None))
    for verdict, code in capture_protocol.PACE_SIGNALS.items():
        found = protocol_hits(AttemptSignals(pace=PaceOutcome(verdict, value=1.0, threshold=2.0)), limits())
        assert [(h.code, h.value, h.threshold) for h in found] == [(code, 1.0, 2.0)]
    assert protocol_hits(AttemptSignals(pace=PaceOutcome(PaceVerdict.PACED)), limits()) == []


def test_the_pulse_is_measured_only_and_never_decides():
    setting = SignalSetting(code="PULSE_ABSENT", kind="PRESENTATION", points=90, mode="ENFORCE")
    config = RiskConfig(
        enabled=True,
        medium=30,
        high=60,
        critical=80,
        medium_action="STEP_UP",
        high_action="REVIEW",
        critical_action="DENY",
        fallback="ALLOW",
        family_cap=60,
        signals={"PULSE_ABSENT": setting},
    )
    decision = decide([Hit("PULSE_ABSENT", -9.0, -1.0)], config)
    assert "PULSE_ABSENT" in MEASURE_ONLY_SIGNALS
    assert (decision.score, decision.action, decision.reasons[0].mode) == (0, "ALLOW", "OBSERVE")


# ---------------------------------------------------------------- en el intento


def verify_with(client, headers, *, frontal=b"face:juan", turn="turn:{person}", burst=None, paced=True):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", frontal, "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "juan", image=turn) + flash_files(challenge, "juan", paced=paced)
    files += burst if burst is not None else burst_files()
    data = {"challenge_id": challenge["challenge_id"], "camera_label": "Cam"}
    return client.post(VERIFY, data=data, files=files, headers=headers), challenge


def metric() -> FaceAttemptMetric:
    with SessionLocal() as db:
        row = db.scalars(select(FaceAttemptMetric).order_by(FaceAttemptMetric.id.desc())).first()
        assert row is not None
        return row


def test_a_live_take_with_its_burst_is_measured_and_only_numbers_are_kept(client, company_headers):
    headers = approved_employee(client, company_headers)
    answer, challenge = verify_with(client, headers)
    assert answer.json()["data"]["verified"] is True
    assert not {code for code in last_reasons() if code.startswith(("BURST", "MOIRE", "NOISE", "PERSPECTIVE"))}
    row = metric()
    assert (row.burst_frames, row.burst_motion, row.pulse_snr) == (HOLDS + MOVES, 2.5, 1.5)
    assert (row.moire, row.noise_ratio) == (10.0, 1.0) and row.parallax is not None and row.parallax > 0.09
    assert challenge["burst"] == {
        "tile": settings.FACE_BURST_TILE_PX,
        "hold": HOLDS,
        "move": MOVES,
        "fps": settings.FACE_BURST_FPS,
        "quality": settings.FACE_BURST_JPEG_QUALITY,
        "margin": settings.FACE_BURST_MARGIN,
        "max_bytes": capture_protocol.max_burst_bytes(),
        "min_frames": settings.FACE_BURST_MIN_FRAMES,
    }


@pytest.mark.parametrize(
    ("burst", "code"),
    [
        ([], "BURST_MISSING"),
        (burst_files(kind="burst-frozen"), "BURST_FROZEN"),
        (burst_files(kind="burst-loop"), "BURST_LOOP"),
        (burst_files(kind="burst-cut"), "BURST_DISCONTINUOUS"),
        (burst_files(kind="burst-nopulse"), "PULSE_ABSENT"),
        (burst_files(kind="burst-broken"), "BURST_MISSING"),
        (burst_files(meta={"v": 1}), "BURST_MISSING"),
    ],
)
def test_each_burst_signal_is_measured_without_rejecting_anyone(client, company_headers, burst, code):
    headers = approved_employee(client, company_headers)
    assert verify_with(client, headers, burst=burst)[0].json()["data"]["verified"] is True
    reason = last_reasons()[code]
    assert reason["mode"] == "OBSERVE"


def test_screens_smooth_faces_and_flat_turns_are_measured(client, company_headers):
    headers = approved_employee(client, company_headers)
    assert verify_with(client, headers, frontal=b"screen:juan")[0].json()["data"]["verified"] is True
    assert last_reasons()["MOIRE_HIGH"]["value"] == 30.0
    assert verify_with(client, headers, frontal=b"smooth:juan")[0].json()["data"]["verified"] is True
    assert last_reasons()["NOISE_MISMATCH"]["value"] == 0.2
    assert verify_with(client, headers, turn="flat-turn:{person}")[0].json()["data"]["verified"] is True
    assert last_reasons()["PERSPECTIVE_FLAT"]["value"] == pytest.approx(0.0, abs=1e-6)


def test_an_oversized_burst_is_a_signal_not_a_413(client, company_headers, monkeypatch):
    headers = approved_employee(client, company_headers)
    monkeypatch.setattr(settings, "FACE_BURST_MAX_MB", 0.000001)
    assert verify_with(client, headers)[0].json()["data"]["verified"] is True
    assert last_reasons()["BURST_MISSING"]["value"] == 1.0


def test_a_burst_engine_failure_leaves_the_burst_unmeasured(client, company_headers, caplog):
    headers = approved_employee(client, company_headers)
    with caplog.at_level(logging.ERROR):
        answer = verify_with(client, headers, burst=burst_files(kind="burst-crash"))[0]
    assert answer.json()["data"]["verified"] is True and "ráfaga" in caplog.text
    assert not {code for code in last_reasons() if code.startswith("BURST")} and metric().burst_frames is None


def test_without_the_burst_policy_nothing_is_asked_or_measured(client, company_headers):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, capture_burst=False)
    answer, challenge = verify_with(client, headers, burst=[])
    assert answer.json()["data"]["verified"] is True and challenge["burst"] is None
    assert "BURST_MISSING" not in last_reasons()


def test_the_burst_sheet_goes_to_the_case_evidence_through_the_same_path(client, company_headers, bucket):
    headers = approved_employee(client, company_headers)
    denied, _ = verify_with(client, headers, frontal=b"spoof:juan")
    assert denied.json()["code"] == "SPOOF_DETECTED"
    with SessionLocal() as db:
        kinds = [row.kind for row in db.scalars(select(FraudEvidence).order_by(FraudEvidence.id))]
    assert kinds.count("BURST") == 1 and kinds[-1] == "BURST"  # en su propio lugar, cifrada en el bucket


# ---------------------------------------------------------------- política del ADMIN


def test_the_protocol_switches_are_admin_policy_and_relaxing_them_needs_a_second_admin(client, company_headers):
    url, admin = admin_policy(client, company_headers)
    policy = client.get(url, headers=admin).json()["data"]
    assert policy["flash_paced"] is True and policy["capture_burst"] is True  # nacen midiendo
    signals = {s["code"]: s for s in policy["risk_signals"]}
    assert signals["PULSE_ABSENT"]["measure_only"] is True and signals["BURST_LOOP"]["measure_only"] is False
    assert all(signals[c]["mode"] == "OBSERVE" for c in ("BURST_MISSING", "FLASH_UNPACED", "PERSPECTIVE_FLAT"))
    off = client.put(url, json={"flash_paced": False}, headers=admin).json()["data"]
    assert off["change"]["status"] == "PENDING" and off["policy"]["flash_paced"] is True  # relajar: dos personas
    refused = client.put(url, json={"risk_signals": {"PULSE_ABSENT": {"mode": "ENFORCE"}}}, headers=admin)
    assert refused.status_code == 422 and refused.json()["code"] == "SIGNAL_MEASURE_ONLY"
    assert client.put(url, json={"risk_signals": {"PULSE_ABSENT": {"mode": "OFF"}}}, headers=admin).status_code == 200
    # El nivel Máximo pide el destello dictado y la ráfaga (sin ellos, un paso más; nunca niega).
    applied = client.post(f"{url}/preset", json={"preset": "MAXIMUM"}, headers=admin).json()["data"]["policy"]
    maximum = {s["code"]: s for s in applied["risk_signals"]}
    assert (maximum["FLASH_UNPACED"]["mode"], maximum["FLASH_UNPACED"]["points"]) == ("ENFORCE", 20)
    assert (maximum["BURST_MISSING"]["mode"], maximum["BURST_MISSING"]["points"]) == ("ENFORCE", 20)
    assert maximum["FLASH_PACE_MISMATCH"]["mode"] == "ENFORCE"


def test_required_protocol_signals_ask_for_one_more_step_never_deny(client, company_headers):
    """Como en el nivel Máximo: sin destello dictado ni ráfaga, un paso más (y superado, pasa)."""
    headers = approved_employee(client, company_headers)
    required = {"mode": "ENFORCE", "points": 20}
    set_policy(client, company_headers, risk_signals={"FLASH_UNPACED": required, "BURST_MISSING": required})
    asked, _ = verify_with(client, headers, burst=[], paced=False)
    assert asked.status_code == 422 and asked.json()["code"] == "STEP_UP_REQUIRED"
    # Aunque pesaran para negar, solo dejan el registro "en revisión" (piden más, nunca niegan).
    heavy = {"mode": "ENFORCE", "points": 100}
    set_policy(client, company_headers, risk_signals={"FLASH_UNPACED": heavy, "BURST_MISSING": heavy})
    reviewed, _ = verify_with(client, headers, burst=[], paced=False)
    assert reviewed.json()["data"]["verified"] is True and reviewed.json()["data"]["review"] is True


# ---------------------------------------------------------------- Seguridad facial


def test_face_security_shows_the_protocol_and_its_calibrated_metrics(
    client, company_headers, admin_headers, monkeypatch
):
    approved_employee(client, company_headers)
    monkeypatch.setattr(settings, "FACE_AUTOCALIBRATION_MIN_SAMPLES", 20)
    add_metrics(
        25,
        success=True,
        steps=2,
        flash_score=0.8,
        flash_pace_ms=600.0,
        burst_frames=40,
        pulse_snr=1.0,
        burst_motion=2.0,
        parallax=0.15,
        noise_ratio=1.0,
        moire=12.0,
    )
    add_metrics(5, success=True, steps=2, flash_score=0.8, flash_pace_ms=2500.0, pulse_snr=-5.0)
    add_metrics(5, success=True, steps=2, flash_score=0.8)
    with SessionLocal() as db:
        face_security.recalibrate(db, datetime.now(UTC))
    overview = client.get(ADMIN_URL, headers=admin_headers).json()["data"]
    assert overview["protocol"] == {
        "flash_attempts": 35,
        "paced": 30,
        "late": 5,
        "pace_p50_ms": 600.0,
        "pace_p95_ms": 2500.0,
        "window_ms": settings.FACE_FLASH_PACE_WINDOW_MS,
        "liveness_attempts": 35,
        "bursts": 25,
        "pulse_measured": 30,
        "pulse_seen": 25,
        "pulse_median_snr": 1.0,
    }
    thresholds = {t["key"]: t for t in overview["thresholds"]}
    moire = thresholds["MOIRE"]
    # Un máximo se endurece BAJANDO: 12 dB ÷ 0.85 ≈ 14.1, entre su piso (lo más estricto) y su valor de partida.
    assert moire["upper"] is True and moire["raised"] is True and moire["value"] == pytest.approx(14.11765)
    assert thresholds["BURST_MOTION"]["value"] == settings.FACE_BURST_MAX_MOTION  # 2.0 × 0.85, topado en 1.5
    assert thresholds["PARALLAX"]["raised"] is True and thresholds["NOISE_RATIO"]["value"] == 0.85
    assert thresholds["LIVENESS_YAW"]["upper"] is False


def test_an_upper_threshold_without_data_starts_at_its_loosest_and_never_passes_its_floor(monkeypatch):
    signal = face_security.SIGNAL_BY_KEY["MOIRE"]
    assert signal.candidate([]) == settings.FACE_MOIRE_MAX_DB and not signal.tightened(signal.baseline())
    monkeypatch.setattr(settings, "FACE_AUTOCALIBRATION_MIN_SAMPLES", 20)
    assert signal.candidate([1.0] * 30) == settings.FACE_MOIRE_TIGHTEST_DB  # nunca bajo su piso
    assert signal.candidate([40.0] * 30) == settings.FACE_MOIRE_MAX_DB  # ni sobre su valor de partida


def test_misordered_protocol_settings_are_rejected_at_startup():
    from app.core.config import Settings

    with pytest.raises(ValueError, match="FACE_PARALLAX_MIN"):
        Settings(FACE_PARALLAX_MIN=0.5, FACE_PARALLAX_MAX=0.1)


def test_a_burst_layout_reports_its_sheet_size():
    layout = BurstLayout(tile=112, cols=8, times=tuple(range(0, 900, 100)), segments=("H",) * 9)
    assert layout.size == (896, 224) and layout.count == 9
