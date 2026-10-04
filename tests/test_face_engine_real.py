"""Motor facial REAL (modelos ONNX/OpenCV de la imagen de Docker) con una foto de dominio público.

El resto de las pruebas usa un motor simulado (rápido y determinista); estas validan que los modelos
reales cargan y responden como espera el pipeline: detección (YuNet), alineación, embeddings (SFace
y la fusión con FaceNet), anti-spoofing (MiniFASNet) y accesorios (CLIP). La imagen de desarrollo
expone los modelos en TEST_FACE_MODELS_DIR (/opt/models).
"""

import os
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.facial_recognition import build_thresholds
from app.facial_recognition.engine import DetectedFace
from app.facial_recognition.matcher import cosine_similarity
from app.facial_recognition.model_store import BUNDLED_MODELS, MODELS, ensure_models
from app.facial_recognition.pipeline import FacePipeline, FacePolicy

MODELS_DIR = os.environ.get("TEST_FACE_MODELS_DIR", "")
pytestmark = pytest.mark.skipif(
    not MODELS_DIR or not Path(MODELS_DIR).exists(), reason="Sin modelos ONNX (córrelas en la imagen de desarrollo)"
)

PHOTO = Path(__file__).parent / "fixtures" / "astronaut.png"


@pytest.fixture(scope="module")
def image() -> np.ndarray:
    picture = cv2.imread(str(PHOTO), cv2.IMREAD_COLOR)
    assert picture is not None
    return picture


@pytest.fixture(scope="module")
def engine():
    from app.facial_recognition.opencv_engine import FusionFaceEngine

    return FusionFaceEngine(MODELS_DIR, auto_download=False)


@pytest.fixture(scope="module")
def face(engine, image) -> DetectedFace:
    found = engine.detect(image, 0.6)
    assert len(found) == 1
    return found[0]


def test_all_models_are_present_and_verified():
    assert set(ensure_models(MODELS_DIR, download=False)) == {m.filename for m in MODELS}
    assert all(m.url is None for m in BUNDLED_MODELS)


def test_detection_and_landmarks(face, image):
    height, width = image.shape[:2]
    assert face.score > 0.8 and 0 <= face.x < width and 0 <= face.y < height and face.width > 80
    assert face.landmarks.eye_a[0] != face.landmarks.eye_b[0]


def test_embeddings_are_normalized_and_stable(engine, face, image):
    aligned = engine.align(image, face)
    assert aligned.shape[:2] == (112, 112)
    first = engine.represent(image, face, aligned)
    again = engine.represent(image, face, engine.align(image, face))
    assert first.shape == (engine.embedding_dim,) and abs(float(np.linalg.norm(first)) - 1) < 1e-3
    assert cosine_similarity(first, again) > 0.9999
    # Solo SFace (sin la fusión): 128 dimensiones.
    sface = engine.embed(aligned)
    assert sface.shape == (128,) and abs(float(np.linalg.norm(sface)) - 1) < 1e-3


def test_slightly_altered_photo_is_the_same_person(engine, face, image):
    brighter = cv2.convertScaleAbs(image, alpha=1.1, beta=10)
    other_face = engine.detect(brighter, 0.6)[0]
    a = engine.represent(image, face, engine.align(image, face))
    b = engine.represent(brighter, other_face, engine.align(brighter, other_face))
    assert cosine_similarity(a, b) > 0.8


def test_antispoof_and_accessories_score_a_real_face(face, image):
    from app.facial_recognition.accessories import ClipAccessoryDetector
    from app.facial_recognition.antispoof import MiniFasAntiSpoof

    real = MiniFasAntiSpoof(MODELS_DIR, auto_download=False).real_probability(image, face)
    assert 0.0 <= real <= 1.0
    scores = ClipAccessoryDetector(MODELS_DIR, auto_download=False).score(image, face)
    assert all(0.0 <= value <= 1.0 for value in (scores.glasses, scores.headwear, scores.mask))


def test_full_pipeline_on_a_real_capture(engine):
    from app.facial_recognition.accessories import ClipAccessoryDetector
    from app.facial_recognition.antispoof import MiniFasAntiSpoof

    thresholds = replace(build_thresholds(), max_yaw_ratio=1.0, max_roll_degrees=45.0)
    pipeline = FacePipeline(
        engine,
        thresholds,
        ClipAccessoryDetector(MODELS_DIR, auto_download=False),
        MiniFasAntiSpoof(MODELS_DIR, auto_download=False),
    )
    # Con accesorios bloqueados se califican (CLIP); sin exigirlos, el resultado se reporta y no se lanza.
    policy = FacePolicy(block_glasses=True, block_headwear=True, block_mask=True, reject_foreign_images=False)
    analysis = pipeline.analyze_frontal(PHOTO.read_bytes(), policy=policy, enforce_accessories=False)
    assert analysis.detection_score > 0.8 and analysis.embedding.shape == (engine.embedding_dim,)
    assert analysis.real_probability is not None and analysis.accessories is not None
    assert pipeline.model_name == engine.model_name
