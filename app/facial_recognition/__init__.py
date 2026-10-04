"""Módulo de reconocimiento facial.

Punto de entrada: `lease_pipeline()` reserva uno de los N workers (1 por núcleo) del pool.
Los modelos se cargan una sola vez y se reutilizan durante toda la vida del proceso.
"""

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict

from app.core.config import settings
from app.facial_recognition.errors import FaceValidationError
from app.facial_recognition.pipeline import Accessory, FaceAnalysis, FacePipeline, FacePolicy, QualityThresholds
from app.facial_recognition.pose import TurnDirection
from app.facial_recognition.worker_pool import QueueFullError, QueueTimeoutError, WorkerPool, available_cpus

logger = logging.getLogger(__name__)


def build_thresholds() -> QualityThresholds:
    return QualityThresholds(
        min_detection_score=settings.FACE_DETECTION_MIN_SCORE,
        secondary_detection_score=settings.FACE_SECONDARY_DETECTION_SCORE,
        min_face_size_px=settings.FACE_MIN_SIZE_PX,
        min_sharpness=settings.FACE_MIN_SHARPNESS,
        min_brightness=settings.FACE_MIN_BRIGHTNESS,
        max_brightness=settings.FACE_MAX_BRIGHTNESS,
        min_image_dimension=settings.MIN_IMAGE_DIMENSION,
        max_image_dimension=settings.MAX_IMAGE_DIMENSION,
        max_yaw_ratio=settings.FACE_MAX_YAW_RATIO,
        max_roll_degrees=settings.FACE_MAX_ROLL_DEGREES,
        glasses_threshold=settings.FACE_GLASSES_THRESHOLD,
        headwear_threshold=settings.FACE_HEADWEAR_THRESHOLD,
        mask_threshold=settings.FACE_MASK_THRESHOLD,
        mask_max_skin_ratio=settings.FACE_MASK_MAX_SKIN_RATIO,
        mask_strict_threshold=settings.FACE_MASK_STRICT_THRESHOLD,
        liveness_min_yaw_ratio=settings.FACE_LIVENESS_MIN_YAW_RATIO,
    )


class FaceEngineUnavailable(RuntimeError):
    """Los modelos no pudieron cargarse; el resto de la API sigue operando."""


class _PipelineHolder:
    """Pool de pipelines (1 por núcleo) con carga perezosa y circuit breaker.

    Si la carga falla (modelo corrupto, sin red para descargarlo, memoria), no se reintenta
    en cada petición: se espera FACE_ENGINE_RETRY_SECONDS. Mientras tanto los endpoints
    faciales responden 503 y el resto (login, QR, administración) sigue funcionando.
    """

    def __init__(self) -> None:
        self._pool: WorkerPool[FacePipeline] | None = None
        self._lock = threading.Lock()
        self._last_failure = 0.0
        self._last_error: str | None = None

    def pool(self) -> WorkerPool[FacePipeline]:
        if self._pool is not None:
            return self._pool
        # Si otro hilo ya está cargando los modelos (segundos, o más con una descarga lenta), esta
        # petición responde 503 de inmediato en vez de bloquear un hilo esperando el candado.
        if not self._lock.acquire(blocking=False):
            raise FaceEngineUnavailable("El motor facial se está iniciando")
        try:
            return self._load()
        finally:
            self._lock.release()

    def _load(self) -> WorkerPool[FacePipeline]:
        """Carga el pool (con el candado tomado) o falla rápido mientras dura la pausa tras un error."""
        if self._pool is not None:
            return self._pool
        if self._last_failure and time.monotonic() - self._last_failure < settings.FACE_ENGINE_RETRY_SECONDS:
            raise FaceEngineUnavailable(self._last_error or "Modelos no disponibles")
        try:
            self._pool = self._build()
            self._last_error = None
            stats = self._pool.stats()
            logger.info("Motor facial listo: %s workers (1 por núcleo), cola máx. %s", stats.workers, stats.max_waiting)
            return self._pool
        except Exception as exc:
            self._last_failure = time.monotonic()
            self._last_error = f"{exc.__class__.__name__}: {exc}"
            logger.exception("No se pudieron cargar los modelos faciales")
            raise FaceEngineUnavailable(self._last_error) from exc

    def status(self) -> dict:
        if self._pool is not None:
            return {"status": "ok", "error": None, "queue": asdict(self._pool.stats())}
        return {"status": "unavailable" if self._last_error else "not_loaded", "error": self._last_error}

    @staticmethod
    def _build() -> WorkerPool[FacePipeline]:
        import cv2

        from app.facial_recognition.opencv_engine import FusionFaceEngine, OpenCVFaceEngine

        # Cada inferencia usa un solo hilo: el paralelismo viene de los workers (1 por núcleo),
        # evitando que varias solicitudes compitan por los mismos núcleos.
        cv2.setNumThreads(1)
        # 1 worker por núcleo EN TOTAL: los núcleos se reparten entre los procesos de la API.
        workers = settings.FACE_WORKERS or max(1, available_cpus() // settings.API_WORKERS)
        max_waiting = settings.FACE_QUEUE_MAX_WAITING or workers * 4

        accessory_detector = None
        if settings.FACE_ACCESSORY_CHECK_ENABLED:
            from app.facial_recognition.accessories import ClipAccessoryDetector

            # Sesión ONNX compartida (thread-safe); cada llamada usa 1 hilo.
            accessory_detector = ClipAccessoryDetector(
                settings.FACE_MODELS_DIR, auto_download=settings.FACE_MODELS_AUTO_DOWNLOAD
            )
        antispoof = None
        if settings.FACE_ANTISPOOF_ENABLED:
            from app.facial_recognition.antispoof import MiniFasAntiSpoof

            antispoof = MiniFasAntiSpoof(settings.FACE_MODELS_DIR, auto_download=settings.FACE_MODELS_AUTO_DOWNLOAD)
        thresholds = build_thresholds()

        def factory(_: int) -> FacePipeline:
            models_dir, download = settings.FACE_MODELS_DIR, settings.FACE_MODELS_AUTO_DOWNLOAD
            engine: OpenCVFaceEngine = (
                FusionFaceEngine(models_dir, download, sface_weight=settings.FACE_FUSION_SFACE_WEIGHT)
                if settings.FACE_RECOGNITION_MODEL == "fusion"
                else OpenCVFaceEngine(models_dir, download)
            )
            return FacePipeline(engine, thresholds, accessory_detector, antispoof)

        return WorkerPool(
            factory, size=workers, max_waiting=max_waiting, wait_timeout=settings.FACE_QUEUE_TIMEOUT_SECONDS
        )


_holder = _PipelineHolder()


def face_pool() -> WorkerPool[FacePipeline]:
    """Pool de workers faciales (lo carga en el primer uso)."""
    return _holder.pool()


@contextmanager
def lease_pipeline() -> Iterator[FacePipeline]:
    """Reserva un worker (FIFO). Lanza QueueFullError / QueueTimeoutError ante saturación."""
    with face_pool().lease() as pipeline:
        yield pipeline


def face_engine_status() -> dict:
    return _holder.status()


__all__ = [
    "Accessory",
    "FaceAnalysis",
    "FaceEngineUnavailable",
    "FacePipeline",
    "FacePolicy",
    "FaceValidationError",
    "QueueFullError",
    "QueueTimeoutError",
    "TurnDirection",
    "face_engine_status",
    "face_pool",
    "lease_pipeline",
]
