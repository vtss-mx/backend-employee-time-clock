"""Piezas comunes de la identificación por rostro y por QR.

- VerificationService (EMPLOYEE): verificación 1:1, el empleado contra su propio rostro o su QR.
- CheckpointService (VALIDATOR): identifica a cualquier empleado de su empresa (rostro 1:N o QR).

Ambos usan las mismas reglas (umbral de confianza de la empresa, prueba de vida, mensajes) y
registran cada intento en la bitácora (attendance.verification_logs). Los mensajes de rechazo y las
instrucciones de la prueba de vida salen de los catálogos de la base (catalog_service).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.facial_recognition import FaceAnalysis, FacePipeline, FacePolicy, FaceValidationError
from app.facial_recognition.calibration import match_confidence, similarity_for_confidence
from app.facial_recognition.matcher import best_match, cosine_similarity
from app.models import Employee, VerificationLog, VerificationMethod
from app.repositories.verification_repository import VerificationLogRepository
from app.schemas.verification import FaceChallengeResponse, VerificationResult
from app.services.capture_guard import ensure_human_timing, spoofed
from app.services.catalog_service import get_catalogs
from app.services.face_service import SECURITY_REASONS, SuspiciousCapture
from app.services.liveness_service import Challenge, challenge_store
from app.services.policy_service import PolicySnapshot

FACE_SUCCESS = "Identificación exitosa"
QR_SUCCESS = "Identificación exitosa"
MAX_FRONTAL_FRAMES = 3


def reason_message(reason: str | None) -> str:
    """Mensaje para la persona según el motivo del rechazo (catalog.verification_reasons)."""
    return get_catalogs().reason_message(reason)


def required_similarity(policy: PolicySnapshot) -> float:
    """Similitud que debe alcanzar CADA captura: la confianza que exige la empresa, nunca por
    debajo del piso técnico FACE_MATCH_THRESHOLD."""
    model = settings.FACE_RECOGNITION_MODEL
    return max(settings.FACE_MATCH_THRESHOLD, similarity_for_confidence(policy.min_confidence, model))


def mean_confidence(similarities: Sequence[float]) -> float:
    """Confianza promedio (7 decimales: con niveles como 99.999 % la diferencia está en la quinta cifra)."""
    model = settings.FACE_RECOGNITION_MODEL
    return round(sum(match_confidence(s, model) for s in similarities) / len(similarities), 7)


def match_references(frontal: Sequence[FaceAnalysis], references: list[np.ndarray], required: float) -> list[float]:
    """Similitud de cada captura contra las muestras de UNA persona (1:1)."""
    return [best_match(f.embedding, references, required).similarity for f in frontal]


def ensure_frame_count(images: Sequence[bytes]) -> None:
    if not 1 <= len(images) <= MAX_FRONTAL_FRAMES:
        raise UnprocessableError(f"Envía entre 1 y {MAX_FRONTAL_FRAMES} imágenes frontales", code="INVALID_FRAME_COUNT")


def issue_challenge(db: Session, user_id: int, policy: PolicySnapshot) -> FaceChallengeResponse:
    """Reto de prueba de vida (uno o dos giros, según la empresa) de uso único para quien lo pide."""
    if not policy.liveness_required:
        return FaceChallengeResponse(liveness_required=False)
    challenge = challenge_store.issue(db, user_id, steps=policy.liveness_steps)
    catalogs = get_catalogs(db)
    instructions = [catalogs.liveness_instruction(d.value) for d in challenge.directions]
    return FaceChallengeResponse(
        liveness_required=True,
        challenge_id=challenge.id,
        action=challenge.direction,
        instruction=instructions[0],
        actions=list(challenge.directions),
        instructions=instructions,
        min_yaw_ratio=settings.FACE_LIVENESS_MIN_YAW_RATIO,
        expires_in=settings.FACE_CHALLENGE_TTL_SECONDS,
    )


@dataclass(frozen=True)
class LivenessFailure:
    """Prueba de vida no superada."""

    #: LIVENESS_FAILED (no se vio el giro o la captura del reto no sirvió) o LIVENESS_MISMATCH.
    reason: str
    score: float | None = None
    #: Error de la captura del reto (sin rostro, borrosa...) cuando fue eso lo que falló.
    capture_error: FaceValidationError | None = None

    @property
    def message(self) -> str:
        """Para la identificación: el motivo de la bitácora (o el error de la captura del reto)."""
        if self.capture_error is not None:
            return get_catalogs().face_error_message(self.capture_error.code, self.capture_error.details)
        return reason_message(self.reason)


@dataclass(frozen=True)
class LivenessCheck:
    """Resultado de la prueba de vida: el fallo (si lo hubo) y las capturas de los giros analizadas."""

    failure: LivenessFailure | None = None
    turns: tuple[FaceAnalysis, ...] = ()
    #: Algún giro (junto con alguna frontal) parece una foto, pantalla o video (según el nivel).
    spoofed: bool = False


def check_liveness(
    pipeline: FacePipeline,
    challenge: Challenge | None,
    challenge_images: Sequence[bytes],
    frontal: Sequence[FaceAnalysis],
    policy: PolicySnapshot,
    face_policy: FacePolicy,
) -> LivenessCheck:
    """Las capturas del reto deben llegar a tiempo humano, mostrar cada giro pedido en orden, no ser
    una pantalla y ser la misma persona de las frontales.

    Lanza SuspiciousCapture si llegaron demasiado rápido o una imagen no es de la cámara; lo demás
    (sin giro, otra persona) es un fallo de la prueba de vida que se registra como tal.
    """
    if challenge is None or not challenge_images:
        return LivenessCheck()
    ensure_human_timing(challenge, policy)
    turns: list[FaceAnalysis] = []
    for direction, image in zip(challenge.directions, challenge_images, strict=True):
        try:
            turned = pipeline.analyze_turn(image, direction, policy=face_policy)
        except FaceValidationError as exc:
            if exc.code in SECURITY_REASONS:
                raise SuspiciousCapture(exc.code, exc.details) from exc
            turn_missing = exc.code == "LIVENESS_TURN_NOT_DETECTED"
            failure = LivenessFailure("LIVENESS_FAILED", capture_error=None if turn_missing else exc)
            return LivenessCheck(failure, tuple(turns))
        turns.append(turned)
        consistency = max(cosine_similarity(turned.embedding, f.embedding) for f in frontal)
        if consistency < settings.FACE_LIVENESS_CONSISTENCY_THRESHOLD:
            return LivenessCheck(LivenessFailure("LIVENESS_MISMATCH", round(consistency, 4)), tuple(turns))
    return LivenessCheck(turns=tuple(turns), spoofed=spoofed(frontal, turns, face_policy))


def failed(method: VerificationMethod, message: str) -> VerificationResult:
    return VerificationResult(verified=False, method=method, message=message)


def succeeded(
    employee: Employee, method: VerificationMethod, message: str, *, confidence: float | None
) -> VerificationResult:
    return VerificationResult(
        verified=True,
        method=method,
        message=message,
        employee_id=employee.id,
        employee_number=employee.employee_number,
        name=employee.full_name,
        confidence=None if confidence is None else round(max(0.0, min(1.0, confidence)), 7),
        verified_at=datetime.now(UTC),
    )


class IdentityLog:
    """Bitácora de intentos de identificación (quién lo hizo, a quién, cómo y con qué resultado)."""

    def __init__(self, db: Session, *, ip: str | None, user_agent: str | None) -> None:
        self.db = db
        self.logs = VerificationLogRepository(db)
        self.ip = ip
        self.user_agent = user_agent

    def record(
        self,
        *,
        company_id: int,
        employee_id: int | None,
        actor_id: int,
        method: VerificationMethod,
        success: bool,
        score: float | None = None,
        reason: str | None = None,
    ) -> None:
        self.logs.add(
            VerificationLog(
                employee_id=employee_id,
                company_id=company_id,
                user_id=actor_id,
                method=method,
                success=success,
                score=score,
                reason=reason,
                ip_address=self.ip,
                user_agent=self.user_agent,
            )
        )
        self.db.commit()
