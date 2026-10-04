"""Piezas del motor facial sin modelos: comparación de vectores, oclusión del rostro, imágenes hostiles,
el motor OpenCV ante salidas degeneradas y el pool de workers que arma cada proceso.

Los modelos reales se prueban en test_face_engine_real.py. Aquí se sustituyen por dobles mínimos para
provocar lo que un modelo real casi nunca produce (ningún rostro, un vector nulo, otro hilo que termina
de cargar justo antes) y para revisar cómo se arma el pool según la configuración.
"""

import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import onnxruntime
import pytest
from PIL import Image

import app.facial_recognition as face_module
from app.core.config import settings
from app.facial_recognition import (
    FaceValidationError,
    _PipelineHolder,
    face_engine_status,
    face_pool,
    lease_pipeline,
    opencv_engine,
)
from app.facial_recognition.engine import DetectedFace, FaceLandmarks
from app.facial_recognition.image_utils import decode_image
from app.facial_recognition.matcher import cosine_similarity
from app.facial_recognition.occlusion import lower_face_skin_ratio
from app.facial_recognition.worker_pool import WorkerPool

# ---------------------------------------------------------------- reglas puras


def test_a_null_vector_resembles_nothing():
    """Un vector nulo (motor dañado) da similitud 0, nunca una división entre cero ni un NaN que pase."""
    vector = np.array([0.6, 0.8], dtype=np.float32)
    assert cosine_similarity(np.zeros(2, dtype=np.float32), vector) == 0.0
    assert cosine_similarity(vector, vector) == pytest.approx(1.0)


def _face_with_eyes_at(eye_y: float) -> DetectedFace:
    """Rostro frontal de 80 px de ancho con los ojos a la altura `eye_y` (nariz y boca debajo)."""
    landmarks = FaceLandmarks(
        eye_a=(60.0, eye_y),
        eye_b=(100.0, eye_y),
        nose=(80.0, eye_y + 25),
        mouth_a=(65.0, eye_y + 45),
        mouth_b=(95.0, eye_y + 45),
    )
    return DetectedFace(
        x=40, y=int(eye_y) - 30, width=80, height=100, score=0.95, landmarks=landmarks, raw=np.zeros(15)
    )


def test_skin_is_not_measured_when_the_forehead_is_out_of_frame():
    """Sin la frente (la referencia de piel bajo la misma luz) no se puede afirmar que haya cubrebocas:
    se devuelve None y el pipeline exige que CLIP esté casi seguro."""
    skin = np.full((200, 160, 3), (95, 140, 200), dtype=np.uint8)  # BGR: tono de piel uniforme
    assert lower_face_skin_ratio(skin, _face_with_eyes_at(90.0)) == pytest.approx(1.0)  # visible: se mide
    assert lower_face_skin_ratio(skin, _face_with_eyes_at(6.0)) is None  # frente fuera de la imagen


def _png_header(width: int, height: int) -> bytes:
    """PNG de pocos bytes que DECLARA medir width x height (una "bomba" de descompresión)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


@pytest.mark.parametrize("side", [12_000, 20_000])  # aviso de bomba (>89 Mpx) y error de bomba (>2x)
def test_a_decompression_bomb_is_rejected_before_decoding(side):
    """Una imagen diminuta que dice medir miles de megapíxeles se rechaza al abrirla (sin reservar la
    memoria de sus píxeles) como IMAGE_TOO_LARGE, igual que una demasiado grande."""
    bomb = _png_header(side, side)
    with pytest.raises(FaceValidationError) as error:
        decode_image(bomb, min_dimension=settings.MIN_IMAGE_DIMENSION, max_dimension=settings.MAX_IMAGE_DIMENSION)
    assert error.value.code == "IMAGE_TOO_LARGE"
    assert error.value.details == {"max_dimension": settings.MAX_IMAGE_DIMENSION}
    assert isinstance(error.value.__cause__, Image.DecompressionBombWarning | Image.DecompressionBombError)


# ---------------------------------------------------------------- motor OpenCV con redes simuladas


@pytest.fixture
def nets(monkeypatch):
    """Redes de OpenCV/ONNX simuladas: lo que devuelve cada una se cambia en el objeto devuelto."""
    state = SimpleNamespace(
        faces=None, input_size=None, feature=np.ones(128, dtype=np.float32), facenet=np.ones(512, dtype=np.float32)
    )

    class Detector:
        def setInputSize(self, size):
            state.input_size = size

        def detect(self, _image):
            return 1, state.faces

    class Recognizer:
        def alignCrop(self, image, _raw):
            return image[:112, :112]

        def feature(self, _aligned):
            return state.feature[None]

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _outputs, _feeds):
            return [state.facenet[None]]

    fake_cv2 = SimpleNamespace(
        FaceDetectorYN=SimpleNamespace(create=lambda *_args: Detector()),
        FaceRecognizerSF=SimpleNamespace(create=lambda *_args: Recognizer()),
        resize=cv2.resize,
    )
    monkeypatch.setattr(opencv_engine, "cv2", fake_cv2)
    monkeypatch.setattr(
        opencv_engine,
        "ensure_models",
        lambda models_dir, download, models: {m.filename: Path(models_dir) / m.filename for m in models},
    )
    monkeypatch.setattr(onnxruntime, "InferenceSession", Session)
    return state


def test_an_image_without_faces_yields_no_detections(nets):
    engine = opencv_engine.OpenCVFaceEngine("/modelos", auto_download=False)
    assert engine.detect(np.zeros((240, 320, 3), dtype=np.uint8), 0.6) == []
    assert nets.input_size == (320, 240)  # el detector se ajusta al tamaño de cada imagen


def test_a_null_embedding_is_an_engine_failure_not_a_face(nets):
    """Un vector de norma cero no se normaliza ni se compara: es una falla del motor (ValueError), que
    el servicio responde como 503 FACE_PROCESSING_ERROR en vez de identificar a alguien con él."""
    fusion = opencv_engine.FusionFaceEngine("/modelos", auto_download=False, sface_weight=0.5)
    image = np.zeros((200, 200, 3), dtype=np.uint8)
    face = _face_with_eyes_at(90.0)
    aligned = fusion.align(image, face)
    healthy = fusion.represent(image, face, aligned)
    assert healthy.shape == (fusion.embedding_dim,) and float(np.linalg.norm(healthy)) == pytest.approx(1.0)

    nets.facenet = np.zeros(512, dtype=np.float32)
    with pytest.raises(ValueError, match="FaceNet"):
        fusion.represent(image, face, aligned)
    nets.feature = np.zeros(128, dtype=np.float32)
    with pytest.raises(ValueError, match="norma cero"):
        fusion.embed(aligned)


# ---------------------------------------------------------------- pool de workers del proceso


class _Engine:
    def __init__(self, models_dir, download, **options):
        self.models_dir, self.download, self.options = models_dir, download, options
        self.model_name = "doble"


class _Fusion(_Engine):
    pass


class _Detector:
    def __init__(self, models_dir, *, auto_download):
        self.models_dir, self.auto_download = models_dir, auto_download


@pytest.fixture
def engine_doubles(monkeypatch):
    """Motores, CLIP y anti-spoofing simulados (cargar los reales tarda y requiere los modelos)."""
    from app.facial_recognition import accessories, antispoof

    monkeypatch.setattr(opencv_engine, "OpenCVFaceEngine", _Engine)
    monkeypatch.setattr(opencv_engine, "FusionFaceEngine", _Fusion)
    monkeypatch.setattr(accessories, "ClipAccessoryDetector", _Detector)
    monkeypatch.setattr(antispoof, "MiniFasAntiSpoof", _Detector)


def _configure(monkeypatch, **values):
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)


def test_the_engine_loads_once_with_one_worker_per_core(engine_doubles, monkeypatch):
    """Fusión con anti-spoofing y accesorios: cada worker tiene su motor; CLIP y MiniFAS se comparten."""
    _configure(
        monkeypatch,
        FACE_WORKERS=2,
        FACE_QUEUE_MAX_WAITING=0,
        FACE_RECOGNITION_MODEL="fusion",
        FACE_ANTISPOOF_ENABLED=True,
        FACE_ACCESSORY_CHECK_ENABLED=True,
    )
    holder = _PipelineHolder()
    assert holder.status() == {"status": "not_loaded", "error": None}

    pool = holder.pool()
    assert holder.pool() is pool  # cargado una vez: las siguientes peticiones no vuelven a cargar
    stats = pool.stats()
    assert (stats.workers, stats.max_waiting) == (2, 8)  # cola automática: 4 por worker
    with pool.lease() as first, pool.lease() as second:
        assert isinstance(first.engine, _Fusion) and first.engine is not second.engine
        assert first.engine.options == {"sface_weight": settings.FACE_FUSION_SFACE_WEIGHT}
        assert first.accessories is second.accessories and isinstance(first.accessories, _Detector)
        assert first.antispoof is second.antispoof and isinstance(first.antispoof, _Detector)
    status = holder.status()
    assert status["status"] == "ok" and status["error"] is None
    assert status["queue"]["workers"] == 2 and status["queue"]["processed"] == 2


def test_sface_without_optional_models_uses_the_available_cores(engine_doubles, monkeypatch):
    """Sin FACE_WORKERS: 1 worker por núcleo disponible; sin anti-spoofing ni CLIP no se cargan."""
    _configure(
        monkeypatch,
        FACE_WORKERS=0,
        FACE_QUEUE_MAX_WAITING=5,
        FACE_RECOGNITION_MODEL="sface",
        FACE_ANTISPOOF_ENABLED=False,
        FACE_ACCESSORY_CHECK_ENABLED=False,
    )
    monkeypatch.setattr(face_module, "available_cpus", lambda: 3)
    pool = _PipelineHolder().pool()
    assert (pool.stats().workers, pool.stats().max_waiting) == (3, 5)
    with pool.lease() as pipeline:
        assert type(pipeline.engine) is _Engine
        assert pipeline.accessories is None and pipeline.antispoof is None


def test_a_pool_loaded_by_another_thread_meanwhile_is_reused(monkeypatch):
    """Otro hilo terminó de cargar entre la primera comprobación y el candado: se usa su pool."""
    holder = _PipelineHolder()
    loaded = WorkerPool(lambda i: f"pipeline-{i}", size=1, max_waiting=1, wait_timeout=1)

    class LoadedMeanwhile:
        def acquire(self, blocking=True):
            holder._pool = loaded  # el otro hilo acaba de soltar el candado con el pool listo
            return True

        def release(self):
            pass

    def must_not_load():
        raise AssertionError("no debe volver a cargar los modelos")

    holder._lock = LoadedMeanwhile()
    monkeypatch.setattr(_PipelineHolder, "_build", staticmethod(must_not_load))
    assert holder.pool() is loaded


def test_a_request_leases_a_worker_and_returns_it(monkeypatch):
    holder = _PipelineHolder()
    holder._pool = WorkerPool(lambda i: f"pipeline-{i}", size=1, max_waiting=1, wait_timeout=1)
    monkeypatch.setattr(face_module, "_holder", holder)

    with lease_pipeline() as pipeline:
        assert pipeline == "pipeline-0"
        assert face_pool().stats().busy == 1 and face_engine_status()["queue"]["busy"] == 1
    assert face_engine_status()["queue"]["busy"] == 0 and face_engine_status()["queue"]["processed"] == 1
