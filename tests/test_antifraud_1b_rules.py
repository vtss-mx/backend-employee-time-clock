"""Antifraude 1b, reglas puras (sin base de datos ni red): el modo del dispositivo y el tope de las señales nuevas en el
motor de riesgo, la telemetría de la toma, las tablas JPEG de las capturas y el nombre amigable de un dispositivo."""

import io

import numpy as np
from PIL import Image

from app.core.config import settings
from app.core.devices import device_label
from app.facial_recognition import jpeg_tables
from app.models import RiskAction, RiskTier
from app.services import risk_rules
from app.services.client_evidence import (
    DeviceCheck,
    DeviceProofInput,
    TelemetryReading,
    parse_telemetry,
    screen_problems,
    telemetry_hits,
    track_problems,
)
from app.services.risk_rules import Hit, SignalSetting
from tests.test_antifraud_rules import SIGNALS, config

PHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1"
)
DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36 Edg/130.0"
#: Señales nuevas en "obligatoria" para probar que como mucho piden más.
NEW = {
    code: SignalSetting(code, kind, 40, "ENFORCE", review_reason=review)
    for code, kind, review in (
        ("AUTOMATION", "INJECTION", "BROWSER"),
        ("TELEMETRY_MISSING", "INJECTION", "BROWSER"),
        ("NETWORK_HOSTING", "INJECTION", "NETWORK"),
        ("DEVICE_SHARED", "BUDDY_PUNCHING", "DEVICE"),
        ("DEVICE_NEW", "BUDDY_PUNCHING", "DEVICE"),
        ("LOCATION_JUMP", "LOCATION", "LOCATION"),
    )
}


def config_1b(**changes) -> risk_rules.RiskConfig:
    return config(signals={**SIGNALS, **NEW}, **changes)


# ---------------------------------------------------------------- tope: las señales nuevas nunca niegan


def test_the_new_signals_alone_can_ask_for_review_but_never_deny():
    hits = [Hit("AUTOMATION"), Hit("TELEMETRY_MISSING"), Hit("DEVICE_SHARED"), Hit("LOCATION_JUMP")]
    decision = risk_rules.decide(hits, config_1b())
    # 60 (inyección, con tope) + 40 + 40 = 100: crítico, pero sin ellas no se negaría → "en revisión".
    assert (decision.score, decision.tier, decision.action) == (100, RiskTier.CRITICAL, RiskAction.REVIEW)
    # Con un nivel alto que niega, tampoco.
    assert risk_rules.decide(hits[:2], config_1b(high_action="DENY")).action == RiskAction.REVIEW


def test_a_denial_that_stands_without_the_new_signals_is_kept():
    hits = [Hit("FLASH_FLAT"), Hit("LOCATION_EDGE"), Hit("AUTOMATION")]  # 50 + 35 sin las nuevas: crítico
    assert risk_rules.decide(hits, config_1b()).action == RiskAction.DENY


def test_the_new_signals_are_marked_ask_only():
    assert risk_rules.BROWSER_SIGNALS <= risk_rules.ASK_ONLY_SIGNALS
    assert "JPEG_TABLE_UNKNOWN" in risk_rules.ASK_ONLY_SIGNALS and "IDENTITY_MISMATCH" in risk_rules.ASK_ONLY_SIGNALS
    assert "SPOOF_PROB_LOW" not in risk_rules.ASK_ONLY_SIGNALS


# ---------------------------------------------------------------- modo del dispositivo (D2)


def test_an_untrusted_device_asks_what_its_mode_demands_even_if_the_signal_is_off():
    off = {**SIGNALS, "DEVICE_NEW": SignalSetting("DEVICE_NEW", "BUDDY_PUNCHING", 15, "OFF", review_reason="DEVICE")}
    step_up = risk_rules.decide([Hit("DEVICE_NEW")], config(signals=off, device_mode="STEP_UP"))
    assert (step_up.action, step_up.device, step_up.reasons) == (RiskAction.STEP_UP, True, ())
    approval = risk_rules.decide([Hit("DEVICE_KEY_MISSING")], config(device_mode="APPROVAL"))
    assert approval.action == RiskAction.REVIEW
    assert risk_rules.review_reasons(approval, config(device_mode="APPROVAL")) == ("DEVICE",)
    # Solo medir: nada que pedir.
    observe = risk_rules.decide([Hit("DEVICE_NEW")], config(device_mode="OBSERVE"))
    assert (observe.action, observe.device) == (RiskAction.ALLOW, False)


def test_passing_the_step_up_trusts_the_device_and_without_liveness_it_goes_to_review():
    cfg = config(device_mode="STEP_UP")
    assert risk_rules.decide([Hit("DEVICE_NEW")], cfg, step_up_done=True).action == RiskAction.ALLOW
    assert risk_rules.decide([Hit("DEVICE_NEW")], cfg, can_step_up=False).action == RiskAction.REVIEW
    # Aprobación de la empresa: un paso más no la sustituye.
    assert risk_rules.decide([Hit("DEVICE_NEW")], config(device_mode="APPROVAL"), step_up_done=True).action == (
        RiskAction.REVIEW
    )


def test_the_device_mode_applies_with_the_engine_off_and_never_lowers_a_stricter_action():
    disabled = risk_rules.decide([Hit("DEVICE_NEW"), Hit("FLASH_FLAT")], config(enabled=False, device_mode="STEP_UP"))
    assert (disabled.score, disabled.action) == (0, RiskAction.STEP_UP)
    strict = risk_rules.decide(
        [Hit("FLASH_FLAT"), Hit("LOCATION_EDGE"), Hit("DEVICE_NEW")], config(device_mode="STEP_UP")
    )
    assert strict.action == RiskAction.DENY
    reasons = risk_rules.review_reasons(
        risk_rules.decide([Hit("FLASH_FLAT"), Hit("DEVICE_NEW")], config(device_mode="APPROVAL")), config()
    )
    assert reasons == ("CAPTURE", "DEVICE")
    # El motivo del dispositivo no se repite si una señal del dispositivo ya lo dio.
    both = risk_rules.decide([Hit("DEVICE_SHARED"), Hit("DEVICE_NEW")], config_1b(device_mode="APPROVAL"))
    assert risk_rules.review_reasons(both, config_1b()) == ("DEVICE",)


def test_stricter_keeps_the_strongest_action():
    assert risk_rules.stricter(RiskAction.ALLOW, None) == RiskAction.ALLOW
    assert risk_rules.stricter(RiskAction.DENY, RiskAction.REVIEW) == RiskAction.DENY
    assert risk_rules.stricter(RiskAction.ALERT, RiskAction.STEP_UP) == RiskAction.STEP_UP


def test_device_check_signals():
    assert DeviceCheck("OBSERVE").hits() == [Hit("DEVICE_KEY_MISSING", 0.0)]
    assert DeviceCheck("OBSERVE", invalid=True).hits() == [Hit("DEVICE_KEY_MISSING", 1.0)]
    assert DeviceCheck("OBSERVE", "h", uses=3).hits() == [Hit("DEVICE_NEW", 3.0)]
    assert DeviceCheck("OBSERVE", "h", trusted=True).hits() == []
    assert DeviceCheck("OBSERVE", "h", trusted=True, shared=3).hits() == [Hit("DEVICE_SHARED", 3.0, 2.0)]
    assert DeviceProofInput().sent is False and DeviceProofInput(nonce="x").sent is True


# ---------------------------------------------------------------- telemetría de la toma


def _telemetry(**changes) -> str:
    import json

    data = {
        "v": 1,
        "webdriver": False,
        "automation": 0,
        "virtual_camera": False,
        "track": {"width": 1280, "height": 720, "frame_rate": 30, "width_max": 1920, "height_max": 1080,
                  "frame_rate_max": 60, "device_id": True},
        "frames": {"count": 60, "mean_ms": 33.4, "cv": 0.08},
        "screen": {"width": 390, "height": 844, "pixel_ratio": 3, "touch_points": 5},
    }  # fmt: skip
    return json.dumps({**data, **changes})


def test_parse_telemetry_is_strict_and_never_raises(monkeypatch):
    assert parse_telemetry(None) == TelemetryReading()
    assert parse_telemetry(_telemetry()).data is not None
    assert parse_telemetry("{no json").invalid
    assert parse_telemetry(_telemetry(extra=1)).invalid  # un campo que no existe
    assert parse_telemetry(_telemetry(v=2)).invalid
    monkeypatch.setattr(settings, "CAPTURE_TELEMETRY_MAX_BYTES", 256)
    assert parse_telemetry(_telemetry()).invalid  # más grande que el tope


def test_a_genuine_capture_raises_no_browser_signal():
    assert telemetry_hits(parse_telemetry(_telemetry()), PHONE_UA) == []
    # Un flujo sin telemetría (registro facial) no la espera.
    assert telemetry_hits(None, PHONE_UA) == []


def test_each_browser_signal():
    hits = telemetry_hits(
        parse_telemetry(
            _telemetry(
                webdriver=True,
                automation=2,
                virtual_camera=True,
                track={"width": 1920, "height": 1080, "frame_rate": 30.0, "width_max": 1280, "device_id": False},
                frames={"count": 90, "mean_ms": 33.333, "cv": 0.001, "clock": "presentation"},
                screen={"width": 1920, "height": 1080, "pixel_ratio": 1, "touch_points": 0},
            )
        ),
        PHONE_UA,
    )
    assert hits == [
        Hit("AUTOMATION", 3.0),
        Hit("VIRTUAL_CAMERA_PRESENT"),
        Hit("TRACK_INCONSISTENT", 2.0),
        Hit("FRAME_TIMING_SYNTHETIC", 0.001, settings.RISK_FRAME_TIMING_MIN_CV),
        Hit("SCREEN_INCOHERENT", 2.0),
    ]
    assert telemetry_hits(TelemetryReading(), PHONE_UA) == [Hit("TELEMETRY_MISSING", 0.0)]
    assert telemetry_hits(TelemetryReading(invalid=True), PHONE_UA) == [Hit("TELEMETRY_MISSING", 1.0)]


def test_track_and_screen_rules():
    assert track_problems(None) == 0
    assert screen_problems(None, PHONE_UA) == 0
    reading = parse_telemetry(_telemetry(screen={"width": 0, "height": 0, "pixel_ratio": 0, "touch_points": 0}))
    assert reading.data is not None and screen_problems(reading.data.screen, DESKTOP_UA) == 1
    # Una computadora sin pantalla táctil es normal; pocos cuadros no bastan para juzgar el ritmo.
    desktop = parse_telemetry(_telemetry(screen={"width": 1920, "height": 1080, "pixel_ratio": 1, "touch_points": 0}))
    assert telemetry_hits(desktop, DESKTOP_UA) == []
    few = parse_telemetry(_telemetry(frames={"count": 5, "mean_ms": 33.3, "cv": 0.0}, track=None, screen=None))
    assert telemetry_hits(few, PHONE_UA) == []


def test_a_phone_held_upright_is_not_an_inconsistent_track():
    """Compatibilidad universal: la orientación del dispositivo no es una cámara que reporta más de lo que puede."""
    upright = {"width": 720, "height": 1280, "frame_rate": 30, "width_max": 1920, "height_max": 1080,
               "frame_rate_max": 30, "device_id": True}  # fmt: skip
    reading = parse_telemetry(_telemetry(track=upright))
    assert reading.data is not None and track_problems(reading.data.track) == 0
    assert telemetry_hits(reading, PHONE_UA) == []
    # Lo que la regla atrapa sigue atrapado: una cámara virtual que dice 1920 × 1080 y solo puede 1280 × 720 (en
    # cualquier orientación), una que pasa sus cuadros por segundo o una pista sin deviceId.
    for track, expected in (
        ({"width": 1920, "height": 1080, "width_max": 1280, "height_max": 720}, 2),
        ({"width": 1080, "height": 1920, "width_max": 1280, "height_max": 720}, 2),
        ({"width": 1280, "height": 720, "frame_rate": 60, "width_max": 1280, "frame_rate_max": 30}, 1),
        ({"width": 1280, "height": 720, "width_max": 1280, "height_max": 720, "device_id": False}, 1),
        # Con un solo lado conocido se compara tal cual (nada que girar).
        ({"width": 1920, "width_max": 1280, "height_max": 720}, 1),
        ({"width": 1280, "height": 720, "width_max": 1920}, 0),
    ):
        data = parse_telemetry(_telemetry(track=track)).data
        assert data is not None and track_problems(data.track) == expected, track


def test_the_frame_rhythm_is_only_judged_with_the_arrival_clock():
    """Compatibilidad universal: con el reloj del dibujo (alineado al refresco de la pantalla) una cámara real en fase
    da intervalos idénticos; esa medición no se puede juzgar y nunca cuenta como sospechosa."""
    exact = {"count": 90, "mean_ms": 33.333, "cv": 0.002}
    for clock in (None, "render"):
        frames = {**exact, "clock": clock} if clock else exact
        assert telemetry_hits(parse_telemetry(_telemetry(frames=frames)), PHONE_UA) == []
    # Con el reloj de llegada, un video o una cámara virtual exactos siguen marcándose; una cámara real varía.
    synthetic = telemetry_hits(parse_telemetry(_telemetry(frames={**exact, "clock": "presentation"})), PHONE_UA)
    assert synthetic == [Hit("FRAME_TIMING_SYNTHETIC", 0.002, settings.RISK_FRAME_TIMING_MIN_CV)]
    real = {"count": 90, "mean_ms": 33.4, "cv": 0.03, "clock": "presentation"}
    assert telemetry_hits(parse_telemetry(_telemetry(frames=real)), PHONE_UA) == []
    few = {"count": 5, "mean_ms": 33.3, "cv": 0.0, "clock": "presentation"}
    assert telemetry_hits(parse_telemetry(_telemetry(frames=few)), PHONE_UA) == []
    assert parse_telemetry(_telemetry(frames={**exact, "clock": "vsync"})).invalid  # un reloj que no existe


# ---------------------------------------------------------------- tablas JPEG


def _jpeg(quality: int) -> bytes:
    image = Image.fromarray((np.random.default_rng(quality).random((48, 64, 3)) * 255).astype("uint8"))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _dqt(table_id: int, values: tuple[int, ...], precision: int = 0) -> bytes:
    width = 2 if precision else 1
    return bytes([precision << 4 | table_id]) + b"".join(v.to_bytes(width, "big") for v in values)


def test_the_encoder_quality_of_a_jpeg():
    assert jpeg_tables.encoder_quality(_jpeg(92)) == 92
    assert jpeg_tables.encoder_quality(_jpeg(75)) == 75
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, "PNG")
    assert jpeg_tables.encoder_quality(buffer.getvalue()) == jpeg_tables.NOT_JPEG
    assert jpeg_tables.encoder_quality(b"face:juan") == jpeg_tables.NOT_JPEG
    assert jpeg_tables.ijg_table(50)[:3] == (16, 11, 12)  # la tabla estándar, en zigzag


def test_jpeg_headers_the_parser_walks():
    q92 = jpeg_tables.ijg_table(92)
    soi, eoi = b"\xff\xd8", b"\xff\xd9"
    # Relleno (0xFF de más), un reinicio, una tabla 1 antes de la 0 en el mismo segmento y de 16 bits.
    wide = soi + b"\xff" + b"\xff\xd0" + _segment(0xDB, _dqt(1, (2,) * 64) + _dqt(0, q92, precision=1)) + eoi
    assert jpeg_tables.encoder_quality(wide) == 92
    # Solo la tabla 1 en el primer segmento; la 0 llega en otro (APP0 de por medio).
    later = soi + _segment(0xDB, _dqt(1, (2,) * 64)) + _segment(0xE0, b"JFIF\x00") + _segment(0xDB, _dqt(0, q92)) + eoi
    assert jpeg_tables.encoder_quality(later) == 92
    # Una tabla de otro codificador (no es del IJG): None (p. ej. Safari).
    assert jpeg_tables.encoder_quality(soi + _segment(0xDB, _dqt(0, (3,) * 64)) + eoi) is None
    # Sin tablas antes del barrido, una tabla cortada o un marcador roto: no es un JPEG que se pueda leer.
    assert jpeg_tables.encoder_quality(soi + _segment(0xDA, b"\x00") + eoi) == jpeg_tables.NOT_JPEG
    assert jpeg_tables.encoder_quality(soi + _segment(0xDB, _dqt(0, (1,) * 10)) + eoi) == jpeg_tables.NOT_JPEG
    assert jpeg_tables.encoder_quality(soi + b"\x00\x00\x00\x00") == jpeg_tables.NOT_JPEG


def test_which_captures_look_like_another_encoder():
    assert jpeg_tables.unexpected([], 92) == (False, None)
    assert jpeg_tables.unexpected([92, 92], 92) == (False, 92)
    assert jpeg_tables.unexpected([None, None], 92) == (False, None)  # Safari: el mismo codificador en todas
    assert jpeg_tables.unexpected([75, 75], 92) == (True, 75)  # PIL u OpenCV
    assert jpeg_tables.unexpected([0], 92) == (True, 0)  # no es un JPEG
    assert jpeg_tables.unexpected([92, None], 92) == (True, None)  # imágenes de fuentes distintas


# ---------------------------------------------------------------- nombre amigable del dispositivo


def test_device_labels_from_the_user_agent():
    assert device_label(PHONE_UA) == "iPhone · Safari"
    assert device_label(DESKTOP_UA) == "Windows · Edge"
    android = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
    assert device_label(android) == "Android · Chrome"
    assert device_label("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0; rv:131.0) Gecko/20100101 Firefox/131.0") == (
        "Mac · Firefox"
    )
    assert device_label("Mozilla/5.0 (X11; Linux x86_64) Chrome/130 Safari/537.36 OPR/114.0") == "Linux · Opera"
    assert device_label("SamsungBrowser/25.0 (Linux; Android 14)") == "Android · Samsung Internet"
    assert device_label("Mozilla/5.0 (iPad; CPU OS 17_0) CriOS/130 Mobile Safari") == "iPad · Chrome"
    assert device_label("Mozilla/5.0 (X11; CrOS x86_64) Chrome/130") == "ChromeOS · Chrome"
    assert device_label("curl/8.0") is None and device_label(None) is None
    assert device_label("UnaTableta (sin navegador)") is None
