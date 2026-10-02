"""Piezas comunes de la identificación por rostro y por QR.

- VerificationService (EMPLOYEE): verificación 1:1, el empleado contra su propio rostro o su QR.
- CheckpointService (VALIDATOR): identifica a cualquier empleado de su empresa (rostro 1:N o QR).

Ambos usan las mismas reglas (umbral de confianza de la empresa, prueba de vida, mensajes) y
registran cada intento en la bitácora (attendance.verification_logs).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.facial_recognition import FaceAnalysis, FacePipeline, FaceValidationError, TurnDirection
from app.facial_recognition.calibration import match_confidence, similarity_for_confidence
from app.facial_recognition.matcher import best_match, cosine_similarity
from app.models import Employee, VerificationLog, VerificationMethod
from app.repositories.verification_repository import VerificationLogRepository
from app.schemas.verification import FaceChallengeResponse, VerificationResult
from app.services.liveness_service import Challenge, challenge_store
from app.services.policy_service import PolicySnapshot

FACE_SUCCESS = "Identificación exitosa"
FACE_FAILED = "Rostro no reconocido"
FACE_LIVENESS_FAILED = "No fue posible verificar tu identidad"
LIVENESS_NOT_DETECTED = "no se detectó el giro de cabeza solicitado"
MAX_FRONTAL_FRAMES = 3
TURN_INSTRUCTIONS = {
    TurnDirection.LEFT: "Gira lentamente la cabeza hacia tu izquierda",
    TurnDirection.RIGHT: "Gira lentamente la cabeza hacia tu derecha",
}
QR_SUCCESS = "Identificación exitosa"
QR_INVALID = "QR inválido"
QR_UNKNOWN = "QR no reconocido"
QR_EXPIRED = "El QR ha expirado. Solicita uno nuevo a tu empresa"


def qr_message(reason: str) -> str:
    """Mensaje para el usuario según el motivo del rechazo del QR."""
    messages = {
        "INVALID_FORMAT": QR_INVALID,
        "EXPIRED": QR_EXPIRED,
        "EMPLOYEE_INACTIVE": "El empleado está desactivado",
    }
    return messages.get(reason, QR_UNKNOWN)


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
    """Reto de prueba de vida (girar la cabeza) de uso único para el usuario autenticado."""
    if not policy.liveness_required:
        return FaceChallengeResponse(liveness_required=False)
    challenge = challenge_store.issue(db, user_id)
    return FaceChallengeResponse(
        liveness_required=True,
        challenge_id=challenge.id,
        action=challenge.direction,
        instruction=TURN_INSTRUCTIONS[challenge.direction],
        min_yaw_ratio=settings.FACE_LIVENESS_MIN_YAW_RATIO,
        expires_in=settings.FACE_CHALLENGE_TTL_SECONDS,
    )


@dataclass(frozen=True)
class LivenessFailure:
    reason: str
    message: str
    score: float | None = None


def liveness_failure(
    pipeline: FacePipeline,
    challenge: Challenge | None,
    challenge_image: bytes | None,
    frontal: Sequence[FaceAnalysis],
) -> LivenessFailure | None:
    """La captura del reto debe mostrar el giro pedido y ser la misma persona de las frontales."""
    if challenge is None or challenge_image is None:
        return None
    try:
        turned = pipeline.analyze_turn(challenge_image, challenge.direction)
    except FaceValidationError as exc:
        reason = LIVENESS_NOT_DETECTED if exc.code == "LIVENESS_TURN_NOT_DETECTED" else exc.message
        return LivenessFailure("LIVENESS_FAILED", f"{FACE_LIVENESS_FAILED}: {reason}")
    consistency = max(cosine_similarity(turned.embedding, f.embedding) for f in frontal)
    if consistency < settings.FACE_LIVENESS_CONSISTENCY_THRESHOLD:
        return LivenessFailure(
            "LIVENESS_MISMATCH",
            f"{FACE_LIVENESS_FAILED}: las capturas no corresponden a la misma persona",
            round(consistency, 4),
        )
    return None


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
