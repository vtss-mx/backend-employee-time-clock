"""Análisis facial, registro (embeddings cifrados) y carga de referencias."""

import logging
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from itertools import combinations
from typing import Any

import cv2
import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_bytes, encrypt_bytes
from app.core.exceptions import ServiceUnavailableError, UnprocessableError
from app.facial_recognition import FaceAnalysis, FacePipeline, FacePolicy, FaceValidationError
from app.facial_recognition.matcher import cosine_similarity, embedding_from_bytes, embedding_to_bytes
from app.facial_recognition.pipeline import Accessory, accessory_consensus
from app.models import FaceEmbedding
from app.repositories.face_repository import FaceEmbeddingRepository
from app.services.catalog_service import get_catalogs

MAX_ENROLL_IMAGES = 5
logger = logging.getLogger(__name__)


def face_rejection(code: str, details: Mapping[str, Any] | None = None, prefix: str = "") -> UnprocessableError:
    """422 de la captura facial con el mensaje del catálogo `face_errors` (el motor solo informa el código)."""
    message = prefix + get_catalogs().face_error_message(code, details)
    return UnprocessableError(message, code=code, details=dict(details) if details else None)


def accessories_rejection(found: Sequence[Accessory | str], prefix: str = "") -> UnprocessableError:
    """422 "Quítate los lentes para continuar": los nombres salen del catálogo de accesorios
    (el motor facial corre en otros procesos, sin base de datos, y solo informa los códigos)."""
    codes = [str(a) for a in found]
    message = prefix + get_catalogs().accessories_message(codes)
    return UnprocessableError(message, code="ACCESSORIES_DETECTED", details={"accessories": codes})


def _run(fn: Callable[[], FaceAnalysis], prefix: str = "") -> FaceAnalysis:
    """Traduce errores de calidad de imagen a HTTP 422 (con código y detalles)."""
    try:
        return fn()
    except FaceValidationError as exc:
        if exc.code == "ACCESSORIES_DETECTED":
            raise accessories_rejection((exc.details or {}).get("accessories", []), prefix) from exc
        raise face_rejection(exc.code, exc.details, prefix) from exc
    except cv2.error as exc:
        logger.warning("OpenCV no pudo procesar la imagen: %s", exc)
        raise face_rejection("INVALID_IMAGE", prefix=prefix) from exc
    except Exception as exc:
        # Un fallo inesperado del motor no debe convertirse en un 500 opaco ni tumbar el servicio.
        logger.exception("Error en el motor de reconocimiento facial")
        raise ServiceUnavailableError(
            get_catalogs().face_error_message("FACE_PROCESSING_ERROR"), code="FACE_PROCESSING_ERROR"
        ) from exc


#: Marca de posible suplantación (catalog.enrollment_flags), junto a las de accesorios.
SPOOF_FLAG = "SPOOF"


def spoof_consensus(analyses: list[FaceAnalysis]) -> bool:
    """La MAYORÍA estricta de las capturas parece una foto/pantalla (probabilidad real muy baja)."""
    suspicious = sum(
        a.real_probability is not None and a.real_probability < settings.FACE_ANTISPOOF_THRESHOLD for a in analyses
    )
    return suspicious * 2 > len(analyses)


def analyze_frames(
    pipeline: FacePipeline,
    images: list[bytes],
    *,
    policy: FacePolicy,
    allow_review: bool = False,
    check_spoof: bool = True,
) -> tuple[list[FaceAnalysis], tuple[str, ...]]:
    """Valida varias capturas frontales y decide accesorios y anti-spoofing POR CONSENSO.

    Calidad, pose y "un solo rostro" se exigen en cada captura. Accesorios y suplantación solo
    se consideran si aparecen en la mayoría estricta de las capturas (un falso positivo aislado
    no bloquea).

    allow_review=True (registro facial, que siempre revisa una persona) no bloquea: devuelve las
    marcas ("MASK", "GLASSES", "SPOOF"...) para mostrarlas al administrador.
    """
    many = len(images) > 1

    def analyze(image: bytes) -> FaceAnalysis:
        return pipeline.analyze_frontal(image, policy=policy, enforce_accessories=False)

    analyses = [_run(partial(analyze, img), f"Foto {i}: " if many else "") for i, img in enumerate(images, start=1)]
    found = accessory_consensus([a.accessories_found for a in analyses])
    spoof = check_spoof and spoof_consensus(analyses)
    if spoof:
        logger.info("Anti-spoofing: probabilidades de rostro real %s", [a.real_probability for a in analyses])
    flags = tuple(a.value for a in found) + ((SPOOF_FLAG,) if spoof else ())
    if allow_review:
        return analyses, flags
    if found:
        raise accessories_rejection(found)
    if spoof:
        raise face_rejection("SPOOF_DETECTED")
    return analyses, flags


class FaceService:
    def __init__(self, db: Session, pipeline: FacePipeline) -> None:
        self.db = db
        self.pipeline = pipeline
        self.repo = FaceEmbeddingRepository(db)

    # ---------- Análisis ----------

    def analyze_frames(
        self, images: list[bytes], *, policy: FacePolicy, allow_review: bool = False
    ) -> tuple[list[FaceAnalysis], tuple[str, ...]]:
        return analyze_frames(self.pipeline, images, policy=policy, allow_review=allow_review)

    def analyze_enrollment(
        self, images: list[bytes], *, policy: FacePolicy, allow_review: bool = False
    ) -> tuple[list[FaceAnalysis], tuple[str, ...]]:
        """Valida cada muestra, los accesorios por consenso y que todas sean la misma persona."""
        if not 1 <= len(images) <= MAX_ENROLL_IMAGES:
            raise UnprocessableError(
                f"Envía entre 1 y {MAX_ENROLL_IMAGES} fotografías del rostro", code="INVALID_SAMPLE_COUNT"
            )
        analyses, flagged = self.analyze_frames(images, policy=policy, allow_review=allow_review)
        for a, b in combinations(analyses, 2):
            if cosine_similarity(a.embedding, b.embedding) < settings.FACE_ENROLL_CONSISTENCY_THRESHOLD:
                raise face_rejection("ENROLL_INCONSISTENT")
        return analyses, flagged

    # ---------- Persistencia ----------

    def store(
        self,
        employee_id: int,
        analyses: list[FaceAnalysis],
        *,
        replace: bool,
        active: bool = True,
        enrollment_id: int | None = None,
    ) -> list[FaceEmbedding]:
        if replace:
            self.repo.delete_all(employee_id)
        else:
            # Conserva como máximo N muestras (las más recientes).
            keep = max(0, settings.FACE_MAX_SAMPLES_PER_EMPLOYEE - len(analyses))
            self.repo.delete_oldest_beyond(employee_id, keep=keep)
        return [
            self.repo.add(
                FaceEmbedding(
                    employee_id=employee_id,
                    embedding_encrypted=encrypt_bytes(embedding_to_bytes(a.embedding)),
                    model_name=self.pipeline.model_name,
                    dimension=int(a.embedding.shape[0]),
                    detection_score=a.detection_score,
                    quality_score=a.quality_score,
                    active=active,
                    enrollment_id=enrollment_id,
                )
            )
            for a in analyses[: settings.FACE_MAX_SAMPLES_PER_EMPLOYEE]
        ]

    def load_references(self, employee_id: int) -> list[np.ndarray]:
        """Embeddings activos del empleado generados con el modelo actual."""
        return [
            embedding_from_bytes(decrypt_bytes(item.embedding_encrypted), item.dimension)
            for item in self.repo.list_active(employee_id, model_name=self.pipeline.model_name)
        ]

    def migrate_from_photo(self, employee_id: int, enrollment_id: int, photo: bytes) -> list[np.ndarray]:
        """Genera el embedding del modelo actual a partir de la foto de referencia aprobada.

        Al cambiar de motor (p. ej. SFace → fusión) los empleados aprobados no tienen que volver
        a registrarse: su foto de referencia (cifrada) ya fue validada por COMPANY.
        """
        permissive = FacePolicy(block_glasses=False, block_headwear=False, block_mask=False, anti_spoofing=False)
        try:
            analysis = self.pipeline.analyze_frontal(photo, policy=permissive, enforce_accessories=False)
        except (FaceValidationError, cv2.error, ValueError) as exc:
            logger.warning("No se pudo migrar el rostro del empleado %s: %s", employee_id, exc)
            return []
        self.store(employee_id, [analysis], replace=False, active=True, enrollment_id=enrollment_id)
        self.db.commit()
        logger.info("Embedding del empleado %s migrado al modelo %s", employee_id, self.pipeline.model_name)
        return [analysis.embedding]

    def count(self, employee_id: int) -> int:
        return self.repo.count_active(employee_id)

    def delete_all(self, employee_id: int) -> None:
        self.repo.delete_all(employee_id)
