"""Señales físicas del protocolo de captura (antifraude fase 2a) como reglas puras: la hoja de la ráfaga, el
micromovimiento, los fotogramas repetidos, los saltos, el pulso por video (POS), el paralaje, el moiré y el ruido del
sensor; y el análisis de la ráfaga en el pipeline con un motor simulado."""

import cv2
import numpy as np
import pytest

from app.facial_recognition import burst
from app.facial_recognition.burst import (
    FLAT_PULSE_DB,
    BurstLayout,
    BurstMalformed,
    decode_sheet,
    landmark_jumps,
    micro_motion,
    moire,
    noise_level,
    noise_ratio,
    parallax,
    pulse,
    repeated_frames,
    skin_rgb,
    tiles_of,
)
from app.facial_recognition.engine import DetectedFace, FaceEngine, FaceLandmarks
from app.facial_recognition.pipeline import FacePipeline
from app.services.capture_protocol import burst_rules
from tests.conftest import FACE_3D, FRONTAL_POINTS, _project
from tests.test_units import THRESHOLDS

RNG = np.random.default_rng(5)
TILE = 112


def jpeg(image: np.ndarray, quality: int = 90) -> bytes:
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return data.tobytes()


def tile(level: int = 120, noise: float = 3.0) -> np.ndarray:
    base = np.full((TILE, TILE, 3), level, np.float32) + RNG.normal(0, noise, (TILE, TILE, 3))
    return np.clip(base, 0, 255).astype(np.uint8)


def sheet(tiles: list[np.ndarray], cols: int = 4) -> np.ndarray:
    rows = -(-len(tiles) // cols)
    out = np.zeros((rows * TILE, cols * TILE, 3), np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        out[r * TILE : (r + 1) * TILE, c * TILE : (c + 1) * TILE] = t
    return out


def layout(count: int, cols: int = 4, holds: int | None = None, every: int = 100) -> BurstLayout:
    holds = count if holds is None else holds
    return BurstLayout(
        tile=TILE,
        cols=cols,
        times=tuple(every * i for i in range(count)),
        segments=tuple("H" * holds + "M" * (count - holds)),
    )


# ---------------------------------------------------------------- la hoja


def test_only_a_jpeg_sheet_with_the_described_size_is_decoded(monkeypatch):
    tiles = [tile() for _ in range(6)]
    data = jpeg(sheet(tiles))
    decoded = decode_sheet(data, layout(6))
    assert decoded.shape == (2 * TILE, 4 * TILE, 3)
    assert len(tiles_of(decoded, layout(6))) == 6 and tiles_of(decoded, layout(6))[5].shape == (TILE, TILE, 3)
    _, png = cv2.imencode(".png", sheet(tiles))
    for raw, reason in (
        (png.tobytes(), "not-jpeg"),
        (b"\xff\xd8" + b"basura" * 10, "unreadable"),
        (jpeg(sheet(tiles, cols=3)), "size"),
    ):
        with pytest.raises(BurstMalformed, match=reason):
            decode_sheet(raw, layout(6))
    monkeypatch.setattr(burst.cv2, "imdecode", lambda *_: None)
    with pytest.raises(BurstMalformed, match="undecodable"):
        decode_sheet(data, layout(6))


# ---------------------------------------------------------------- continuidad y movimiento


def test_a_frozen_stream_has_no_micro_motion_and_a_loop_repeats_far_frames():
    live = [burst.gray(tile()) for _ in range(8)]
    assert micro_motion(live) is not None and micro_motion(live) > 1.0
    assert micro_motion(live[:1]) is None
    frozen = [live[0]] * 8
    assert micro_motion(frozen) == 0.0 and repeated_frames(frozen, 0.05) == 0  # congelada no es un bucle
    loop = live[:3] * 3
    assert repeated_frames(loop, 0.05) == 6  # cada fotograma después del primer ciclo repite uno anterior
    assert repeated_frames(live, 0.05) == 0 and repeated_frames(live[:2], 0.05) == 0


def test_a_jump_between_consecutive_crops_breaks_continuity():
    still = FRONTAL_POINTS
    assert landmark_jumps([still, still + 1.0, None, still], 0.8) == 0  # temblor y un recorte sin rostro
    assert landmark_jumps([still, still + 200.0], 0.8) == 1  # saltó de lugar
    assert landmark_jumps([still, (still - still.mean(axis=0)) * 2 + still.mean(axis=0)], 0.8) == 1  # de tamaño


def test_skin_is_read_from_cheeks_and_nose():
    image = np.zeros((TILE, TILE, 3), np.uint8)
    image[:, :] = (40, 90, 160)  # BGR
    assert skin_rgb(image, (20, 20, 70, 70)) == (160.0, 90.0, 40.0)
    assert skin_rgb(image, (200, 200, 10, 10)) is None


# ---------------------------------------------------------------- pulso por video (POS)


def series(beat: float, seconds: float = 3.0, rate: float = 10.0) -> tuple[list, list]:
    times = [int(1000 * i / rate) for i in range(int(seconds * rate))]
    colors = []
    for t in times:
        wave = beat * np.sin(2 * np.pi * 1.2 * t / 1000)
        noise = RNG.normal(0, 0.05, 3)
        colors.append((150 * (1 - 0.3 * wave) + noise[0], 100 * (1 + wave) + noise[1], 80 + noise[2]))
    return colors, times


def test_the_pulse_stands_out_only_when_the_skin_beats():
    beating = pulse(*series(0.01), rate=15, low_hz=0.7, high_hz=3.0)
    still = pulse(*series(0.0), rate=15, low_hz=0.7, high_hz=3.0)
    assert beating is not None and still is not None
    assert beating.snr_db > still.snr_db and abs(beating.bpm - 72) < 10
    flat = pulse([(100.0, 100.0, 100.0)] * 20, list(range(0, 2000, 100)), rate=15, low_hz=0.7, high_hz=3.0)
    assert flat is not None and (flat.snr_db, flat.bpm) == (FLAT_PULSE_DB, 0.0)
    assert pulse([(1.0, 1.0, 1.0)] * 5, [0, 1, 2, 3, 4], rate=15, low_hz=0.7, high_hz=3.0) is None  # muy pocos
    short = pulse([(1.0, 2.0, 3.0)] * 9, list(range(0, 90, 10)), rate=15, low_hz=0.7, high_hz=3.0)
    assert short is None  # 90 ms no dan una serie
    assert pulse([(1.0, 2.0, 3.0)] * 9, [0] * 8, rate=15, low_hz=0.7, high_hz=3.0) is None  # largos distintos


# ---------------------------------------------------------------- paralaje, moiré y ruido


def test_a_real_face_leaves_the_nose_out_of_the_plane_and_a_photo_does_not():
    turned = _project(FACE_3D, yaw=25)
    plane = FACE_3D * np.array([1, 1, 0])
    assert parallax(FRONTAL_POINTS, turned) > 0.12
    assert parallax(_project(plane), _project(plane, yaw=40)) < 0.03


def face_image(side: int = 640, face: int = 200, grating: bool = False, smooth: bool = False) -> np.ndarray:
    image = np.clip(RNG.normal(120, 6, (side, side)), 0, 255).astype(np.float32)
    x = y = (side - face) // 2
    if smooth:
        image[y : y + face, x : x + face] = cv2.GaussianBlur(image[y : y + face, x : x + face], (0, 0), 3)
    if grating:
        yy, xx = np.indices((face, face))
        stripes = 25 * np.sin(2 * np.pi * (xx * np.cos(0.3) + yy * np.sin(0.3)) / 3.3)
        image[y : y + face, x : x + face] += stripes
    return cv2.cvtColor(np.clip(image, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


def test_a_screen_pattern_stands_out_in_the_face_spectrum():
    box = (220, 220, 200, 200)
    natural = moire(face_image(), box)
    screen = moire(face_image(grating=True), box)
    assert natural is not None and screen is not None and screen > natural + 10
    small_face = moire(face_image(face=100), (270, 270, 100, 100))  # rostro chico: parche de 64
    assert small_face is not None
    assert moire(face_image(), (0, 0, 40, 40)) is None  # muy chico para medirlo
    assert moire(face_image(side=60, face=50), (5, 5, 50, 50)) is None  # el parche no cabe en la imagen
    assert moire(np.full((400, 400, 3), 128, np.uint8), box) is None  # sin textura: nada que medir


def test_a_face_smoother_than_its_background_has_inconsistent_noise():
    box = (220, 220, 200, 200)
    real = noise_ratio(face_image(), box, min_background=0.2)
    smooth = noise_ratio(face_image(smooth=True), box, min_background=0.2)
    assert real is not None and smooth is not None and 0.7 < real < 1.3 and smooth < 0.4
    assert noise_ratio(face_image(), (0, 0, 640, 640), min_background=0.2) is None  # sin fondo a los lados
    flat = np.full((640, 640, 3), 120, np.uint8)
    assert noise_ratio(flat, box, min_background=0.2) is None  # fondo sin ruido medible
    assert noise_level(np.zeros((4, 4), np.float32)) is None


# ---------------------------------------------------------------- en el pipeline


class TileEngine(FaceEngine):
    """Un rostro de 70 px en cada recorte, salvo los muy oscuros (sin rostro); los puntos siguen el brillo (un recorte
    "movido" los desplaza)."""

    model_name = "tiles"
    embedding_dim = 4

    def detect(self, image_bgr, min_score):
        level = float(image_bgr.mean())
        if level < 20:
            return []
        shift = 30.0 if level > 200 else 0.0
        lm = FaceLandmarks((40 + shift, 45), (70 + shift, 45), (55 + shift, 60), (43 + shift, 75), (67 + shift, 75))
        return [DetectedFace(x=20, y=20, width=70, height=70, score=0.9, landmarks=lm, raw=np.zeros(15))]

    def align(self, image_bgr, detected):
        return np.zeros((112, 112, 3), np.uint8)

    def embed(self, aligned_face_bgr):
        return np.array([1, 0, 0, 0], dtype=np.float32)


def test_the_pipeline_measures_a_burst_with_the_worker_it_already_has():
    pipeline = FacePipeline(TileEngine(), THRESHOLDS)
    tiles = [tile() for _ in range(30)] + [tile(level=230), tile(level=5)]  # el último: sin rostro
    analysis = pipeline.analyze_burst(jpeg(sheet(tiles, cols=8)), layout(32, cols=8, holds=30), burst_rules())
    assert (analysis.frames, analysis.faceless, analysis.repeats) == (32, 1, 0)
    assert analysis.jumps == 0  # cada tramo se revisa aparte: el recorte desplazado abre el de movimiento
    assert analysis.motion is not None and analysis.motion > 1.0
    assert analysis.pulse is not None and len(analysis.anchors) == 2
    short = pipeline.analyze_burst(jpeg(sheet(tiles[:8])), layout(8), burst_rules())
    assert short.pulse is None and len(short.anchors) == 1  # 0.7 s no alcanzan para el pulso; sin movimiento
    dark = [tile(level=5) for _ in range(8)]
    blind = pipeline.analyze_burst(jpeg(sheet(dark)), layout(8), burst_rules())
    assert (blind.faceless, blind.pulse, blind.anchors) == (8, None, ())
