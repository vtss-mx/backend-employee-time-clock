"""Análisis facial, registro (embeddings cifrados) y carga de referencias."""

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from itertools import combinations
from typing import Any

import cv2
import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import encrypt_bytes, try_decrypt
from app.core.exceptions import ServiceUnavailableError, UnprocessableError
from app.facial_recognition import FaceAnalysis, FacePipeline, FacePolicy, FaceValidationError
from app.facial_recognition.matcher import cosine_similarity, embedding_from_bytes, embedding_to_bytes
from app.facial_recognition.pipeline import Accessory, accessory_consensus
from app.models import Employee, EnrollmentStatus, FaceEmbedding
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.services.catalog_service import get_catalogs

MAX_ENROLL_IMAGES = 5
logger = logging.getLogger(__name__)


def face_rejection(code: str, details: Mapping[str, Any] | None = None, prefix: str = "") -> UnprocessableError:
    """422 de la captura facial con el mensaje del catálogo `face_errors` (el motor solo informa el código)."""
    message = prefix + get_catalogs().face_error_message(code, details)
    return UnprocessableError(message, code=code, details=dict(details) if details else None)


#: Rechazos que indican un intento de engaño (no un problema de calidad de la captura): además de
#: responder 422, se registran en la bitácora con su motivo y cuentan para el bloqueo temporal.
SECURITY_REASONS = (
    "SPOOF_DETECTED",
    "IMAGE_NOT_FROM_CAMERA",
    "STATIC_CAPTURE",
    "REPLAY_DETECTED",
    "CAPTURE_INCONSISTENT",
    "VIRTUAL_CAMERA",
    "CHALLENGE_TOO_FAST",
)


class SuspiciousCapture(UnprocessableError):
    """422 de una captura que parece un intento de engaño (foto, pantalla, reenvío, inyección...).

    Quien atiende la petición la registra en la bitácora (motivo = `code`) antes de responder.
    """

    def __init__(self, code: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(
            get_catalogs().face_error_message(code, details), code=code, details=dict(details) if details else None
        )


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
        if exc.code in SECURITY_REASONS:  # p. ej. una imagen con metadatos de cámara (no es de la app)
            raise SuspiciousCapture(exc.code, exc.details) from exc
        if exc.code == "ACCESSORIES_DETECTED":
            raise accessories_rejection((exc.details or {}).get("accessories", []), prefix) from exc
        raise face_rejection(exc.code, exc.details, prefix) from exc
    except cv2.error as exc:
        logger.warning("OpenCV no pudo procesar la imagen: %s", exc)
        raise face_rejection("INVALID_IMAGE", prefix=prefix) from exc
    except Exception as exc:
        raise engine_failure() from exc


def engine_failure() -> ServiceUnavailableError:
    """Un fallo inesperado del motor (ONNX, memoria...) es un 503 con su código, nunca un 500 opaco
    ni la caída del servicio. Se llama desde un `except` (registra el stack trace)."""
    logger.exception("Error en el motor de reconocimiento facial")
    return ServiceUnavailableError(
        get_catalogs().face_error_message("FACE_PROCESSING_ERROR"), code="FACE_PROCESSING_ERROR"
    )


#: Marca de posible suplantación (catalog.enrollment_flags), junto a las de accesorios.
SPOOF_FLAG = "SPOOF"


def looks_spoofed(analysis: FaceAnalysis, policy: FacePolicy) -> bool:
    """Esta captura parece una foto, una pantalla o un video (según el nivel de la empresa)."""
    return analysis.real_probability is not None and analysis.real_probability < policy.spoof_threshold


def spoof_consensus(analyses: list[FaceAnalysis], policy: FacePolicy) -> bool:
    """Las capturas parecen foto/pantalla: la mayoría estricta o, en el nivel máximo, una sola."""
    suspicious = sum(looks_spoofed(a, policy) for a in analyses)
    return suspicious > 0 if policy.spoof_any_frame else suspicious * 2 > len(analyses)


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
    spoof = check_spoof and spoof_consensus(analyses, policy)
    if spoof:
        logger.info("Anti-spoofing: probabilidades de rostro real %s", [a.real_probability for a in analyses])
    flags = tuple(a.value for a in found) + ((SPOOF_FLAG,) if spoof else ())
    if allow_review:
        return analyses, flags
    if found:
        raise accessories_rejection(found)
    if spoof:
        raise SuspiciousCapture("SPOOF_DETECTED")
    return analyses, flags


def readable_embedding(sample_id: int, encrypted: bytes, dimension: int) -> np.ndarray | None:
    """El vector de una muestra, o None (y un registro en el log) si es ilegible."""
    data = try_decrypt(encrypted)
    vector = None
    if data is not None:
        try:
            vector = embedding_from_bytes(data, dimension)
        except ValueError:
            vector = None
    if vector is None:
        logger.error("Muestra facial %s ilegible (dañada o de otra llave de cifrado): se omite", sample_id)
    return vector


#: Empleados cuya foto aprobada no se pudo migrar al modelo actual, y hasta cuándo no se reintenta:
#: sin esto, cada identificación 1:N volvería a analizar las mismas fotos que ya fallaron.
_MIGRATION_RETRY_SECONDS = 3600.0
_MIGRATION_TRACKED_MAX = 10_000
_migration_failures: dict[int, float] = {}


def block_migration(employee_id: int) -> None:
    if len(_migration_failures) >= _MIGRATION_TRACKED_MAX:
        _migration_failures.clear()
    _migration_failures[employee_id] = time.monotonic() + _MIGRATION_RETRY_SECONDS


def migration_blocked(employee_id: int) -> bool:
    until = _migration_failures.get(employee_id)
    if until is None:
        return False
    if time.monotonic() >= until:
        _migration_failures.pop(employee_id, None)
        return False
    return True


def blocked_migrations() -> set[int]:
    """Empleados que no se reintentan por ahora (para excluirlos de la migración por lotes)."""
    return {employee_id for employee_id in list(_migration_failures) if migration_blocked(employee_id)}


def clear_migration_blocks() -> None:
    """Olvida los bloqueos (cada prueba empieza sin ellos)."""
    _migration_failures.clear()


@dataclass(frozen=True)
class Reference:
    """Una muestra del rostro de un empleado, ya descifrada, con lo que el aprendizaje necesita."""

    id: int
    vector: np.ndarray
    #: Aprendida del uso (False = del registro aprobado: el ancla, nunca se reemplaza).
    learned: bool
    created_at: datetime
    #: Última vez que fue la más parecida en una identificación exitosa.
    last_matched_at: datetime | None = None


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
        active: bool = True,
        enrollment_id: int | None = None,
        learned: bool = False,
    ) -> list[FaceEmbedding]:
        """Guarda las muestras (cifradas). Reemplazar un registro lo decide quien llama (`delete_all`);
        las aprendidas y su lugar los administra face_learning."""
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
                    learned=learned,
                )
            )
            for a in analyses[: settings.FACE_MAX_SAMPLES_PER_EMPLOYEE]
        ]

    def load_references(self, employee_id: int) -> list[Reference]:
        """Muestras activas del empleado generadas con el modelo actual (las aprobadas y las aprendidas).

        Una muestra ilegible (dañada o de una llave de cifrado que ya no se tiene) se omite y se
        registra: el empleado se compara con las demás en vez de fallar toda la verificación."""
        references = []
        for item in self.repo.list_active(employee_id, model_name=self.pipeline.model_name):
            vector = readable_embedding(item.id, item.embedding_encrypted, item.dimension)
            if vector is not None:
                references.append(Reference(item.id, vector, item.learned, item.created_at, item.last_matched_at))
        return references

    def references_for(self, employee: Employee) -> list[Reference]:
        """Muestras del empleado con el modelo actual; si no tiene (cambió el motor), se generan una
        vez desde su foto de referencia aprobada. Vacío = aún no tiene un rostro con qué comparar."""
        references = self.load_references(employee.id)
        if references:
            return references
        if migration_blocked(employee.id):
            return []
        enrollment = FaceEnrollmentRepository(self.db, employee.company_id).latest_for_employee(employee.id)
        if enrollment is None or enrollment.status != EnrollmentStatus.APPROVED or enrollment.photo_encrypted is None:
            # Sin foto aprobada de dónde migrar: no se vuelve a buscar en cada identificación (ni frena
            # la migración por lotes del resto de la empresa).
            block_migration(employee.id)
            return []
        photo = try_decrypt(enrollment.photo_encrypted)
        if photo is None:
            logger.error("La foto aprobada del empleado %s es ilegible: no se puede migrar su rostro", employee.id)
            block_migration(employee.id)
            return []
        return self.migrate_from_photo(employee.id, enrollment.id, photo)

    def migrate_from_photo(self, employee_id: int, enrollment_id: int, photo: bytes) -> list[Reference]:
        """Genera el embedding del modelo actual a partir de la foto de referencia aprobada.

        Al cambiar de motor (p. ej. SFace → fusión) los empleados aprobados no tienen que volver
        a registrarse: su foto de referencia (cifrada) ya fue validada por COMPANY.
        """
        # La foto de referencia ya fue validada (y es interna): sin accesorios, anti-spoofing ni EXIF.
        permissive = FacePolicy(
            block_glasses=False,
            block_headwear=False,
            block_mask=False,
            anti_spoofing=False,
            reject_foreign_images=False,
        )
        try:
            analysis = self.pipeline.analyze_frontal(photo, policy=permissive, enforce_accessories=False)
        except (FaceValidationError, cv2.error, ValueError) as exc:
            logger.warning("No se pudo migrar el rostro del empleado %s: %s", employee_id, exc)
            block_migration(employee_id)
            return []
        except Exception:  # falla inesperada del motor: se registra y la identificación sigue sin él
            logger.exception("El motor falló al migrar el rostro del empleado %s", employee_id)
            block_migration(employee_id)
            return []
        self.store(employee_id, [analysis], active=True, enrollment_id=enrollment_id)
        self.db.commit()
        logger.info("Embedding del empleado %s migrado al modelo %s", employee_id, self.pipeline.model_name)
        return self.load_references(employee_id)

    def delete_all(self, employee_id: int) -> None:
        self.repo.delete_all(employee_id)
