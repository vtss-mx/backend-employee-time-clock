"""Piezas comunes de la identificación por rostro y por QR.

- VerificationService (EMPLOYEE): verificación 1:1, el empleado contra su propio rostro o su QR.
- CheckpointService (VALIDATOR): identifica a cualquier empleado de su empresa (rostro 1:N o QR).

Ambos usan las mismas reglas (umbral de confianza de la empresa, prueba de vida, mensajes) y
registran cada intento en la bitácora (attendance.verification_logs). Los mensajes de rechazo y las
instrucciones de la prueba de vida salen de los catálogos de la base (catalog_service).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean

import cv2
import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.facial_recognition import (
    FaceAnalysis,
    FacePipeline,
    FacePolicy,
    FaceValidationError,
    LivenessAction,
    StepTarget,
)
from app.facial_recognition.calibration import match_confidence, model_floor_confidence, similarity_for_confidence
from app.facial_recognition.matcher import MatchRequirement, acceptance, cosine_similarity
from app.facial_recognition.photometry import FlashResponse, flash_hex, flash_response
from app.facial_recognition.pipeline import FlashCapture
from app.models import Employee, FlashMode, VerificationLog, VerificationMethod
from app.repositories.face_security_repository import FaceSecurityRepository
from app.repositories.verification_repository import VerificationLogRepository
from app.schemas.verification import FaceChallengeResponse, VerificationResult
from app.services import face_signals
from app.services.capture_guard import ensure_human_timing, ensure_real_camera, inspect_take, spoofed
from app.services.catalog_service import get_catalogs
from app.services.face_security import SecurityThresholds, thresholds, under_attack
from app.services.face_service import SECURITY_REASONS, Reference, SuspiciousCapture, engine_failure
from app.services.liveness_service import MAX_STEPS, Challenge, LivenessResponse, challenge_store
from app.services.policy_service import PolicySnapshot

logger = logging.getLogger(__name__)

FACE_SUCCESS = "Identificación exitosa"
QR_SUCCESS = "Identificación exitosa"
MAX_FRONTAL_FRAMES = 3


def reason_message(reason: str | None) -> str:
    """Mensaje para la persona según el motivo del rechazo (catalog.verification_reasons)."""
    return get_catalogs().reason_message(reason)


def _confidence(policy: PolicySnapshot, among_all: bool) -> float:
    """`among_all`: identificar entre toda la plantilla (1:N) usa su propia confianza, nunca menor que
    la de verificar a una persona (buscar entre muchos multiplica las coincidencias falsas)."""
    return max(policy.min_confidence, policy.identify_confidence) if among_all else policy.min_confidence


def required_similarity(policy: PolicySnapshot, *, among_all: bool = False) -> float:
    """Similitud que debe alcanzar CADA captura: la confianza que exige la empresa, nunca por
    debajo del piso técnico FACE_MATCH_THRESHOLD."""
    confidence = _confidence(policy, among_all)
    return max(settings.FACE_MATCH_THRESHOLD, similarity_for_confidence(confidence, settings.FACE_RECOGNITION_MODEL))


def required_match(policy: PolicySnapshot, *, among_all: bool = False) -> MatchRequirement:
    """La similitud de la fusión y, con la fusión, el piso de cada modelo por separado
    (`model_floor_confidence`): ambos modelos deben reconocer a la persona, no solo su promedio."""
    fused = required_similarity(policy, among_all=among_all)
    if settings.FACE_RECOGNITION_MODEL != "fusion":
        return MatchRequirement(fused)
    floor = model_floor_confidence(_confidence(policy, among_all))
    return MatchRequirement(
        fused, (similarity_for_confidence(floor, "sface"), similarity_for_confidence(floor, "facenet"))
    )


def mean_confidence(similarities: Sequence[float]) -> float:
    """Confianza promedio (7 decimales: con niveles como 99.999 % la diferencia está en la quinta cifra)."""
    model = settings.FACE_RECOGNITION_MODEL
    return round(sum(match_confidence(s, model) for s in similarities) / len(similarities), 7)


@dataclass(frozen=True)
class OneToOne:
    """Resultado de comparar las capturas contra las muestras de UNA persona."""

    #: CADA captura alcanzó la similitud que exige la empresa.
    matched: bool
    #: Confianza promedio (para la bitácora y la respuesta).
    score: float
    #: Muestra que más se pareció a alguna captura: la que decidió (suma a su utilidad).
    closest: int | None = None


def match_one(frontal: Sequence[FaceAnalysis], references: Sequence[Reference], policy: PolicySnapshot) -> OneToOne:
    """Comparación 1:1 (el empleado que se verifica, o el dueño del QR en QR + rostro) contra todas
    sus muestras: las del registro aprobado y las que aprendió del uso (face_learning)."""
    scores, accepted = acceptance(
        [f.embedding for f in frontal], [r.vector for r in references], required_match(policy)
    )
    similarities = [round(float(s), 4) for s in scores.max(axis=1)]
    closest = references[int(np.argmax(scores.max(axis=0)))].id
    return OneToOne(bool(accepted.any(axis=1).all()), mean_confidence(similarities), closest)


def ensure_frame_count(images: Sequence[bytes]) -> None:
    if not 1 <= len(images) <= MAX_FRONTAL_FRAMES:
        raise UnprocessableError(f"Envía entre 1 y {MAX_FRONTAL_FRAMES} imágenes frontales", code="INVALID_FRAME_COUNT")


def issue_challenge(db: Session, user_id: int, policy: PolicySnapshot, company_id: int) -> FaceChallengeResponse:
    """Reto de prueba de vida de uso único para quien lo pide: los movimientos al azar (los que pide la
    empresa, o el máximo si está bajo ataque), los colores del destello y los umbrales vigentes (la app
    guía a la persona hasta ellos)."""
    if not policy.liveness_required:
        return FaceChallengeResponse(liveness_required=False)
    steps = MAX_STEPS if under_attack(db, company_id, datetime.now(UTC)) else policy.liveness_steps
    flash = settings.FACE_FLASH_COLORS if policy.flash_liveness != FlashMode.OFF else 0
    challenge = challenge_store.issue(
        db, user_id, steps=steps, lifetime_seconds=policy.liveness_timeout_seconds, flash=flash
    )
    limits = thresholds(db)
    catalogs = get_catalogs()
    instructions = [catalogs.liveness_instruction(a.value) for a in challenge.actions]
    return FaceChallengeResponse(
        liveness_required=True,
        challenge_id=challenge.id,
        action=challenge.actions[0],
        instruction=instructions[0],
        actions=list(challenge.actions),
        instructions=instructions,
        min_yaw_ratio=limits.min_yaw_ratio,
        min_pitch_delta=limits.min_pitch_delta,
        min_closer_scale=limits.min_closer_scale,
        flash=[flash_hex(code) for code in challenge.flash],
        flash_required=policy.flash_liveness == FlashMode.ENFORCE,
        expires_in=policy.liveness_timeout_seconds,
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
    """Resultado de la prueba de vida: el fallo (si lo hubo) y las capturas de los pasos analizadas."""

    failure: LivenessFailure | None = None
    turns: tuple[FaceAnalysis, ...] = ()
    #: Algún paso (junto con alguna frontal) parece una foto, pantalla o video (según el nivel).
    spoofed: bool = False
    #: Los movimientos que se pidieron (en el orden de `turns`).
    actions: tuple[LivenessAction, ...] = ()
    #: Fotogramas del destello que cuentan para la toma única (solo con el destello obligatorio).
    flash: tuple[FlashCapture, ...] = ()


def step_target(frontal: Sequence[FaceAnalysis], limits: SecurityThresholds) -> StepTarget:
    """Lo que debe mostrar cada paso, medido contra las frontales de la misma toma (la persona en reposo)."""
    pitches = [f.pose.pitch_ratio for f in frontal if f.pose is not None]
    return StepTarget(
        min_yaw_ratio=limits.min_yaw_ratio,
        min_pitch_delta=limits.min_pitch_delta,
        min_closer_scale=limits.min_closer_scale,
        baseline_pitch=fmean(pitches) if pitches else 0.5,
        baseline_width=fmean(f.face_box[2] for f in frontal) if frontal else 1.0,
    )


def _analyze_step(
    pipeline: FacePipeline, image: bytes, action: LivenessAction, target: StepTarget, face_policy: FacePolicy
) -> FaceAnalysis | LivenessFailure:
    """Un paso del reto: su captura analizada, o el fallo de la prueba de vida (no se vio el movimiento
    o la captura no sirvió). Una imagen que no es de la cámara es un intento de engaño."""
    try:
        return pipeline.analyze_step(image, action, target, policy=face_policy)
    except FaceValidationError as exc:
        if exc.code in SECURITY_REASONS:
            raise SuspiciousCapture(exc.code, exc.details) from exc
        step_missing = exc.code == "LIVENESS_STEP_NOT_DETECTED"
        return LivenessFailure("LIVENESS_FAILED", capture_error=None if step_missing else exc)
    except cv2.error:  # captura del paso que OpenCV no puede leer: como cualquier captura inválida
        return LivenessFailure("LIVENESS_FAILED", capture_error=FaceValidationError("INVALID_IMAGE"))
    except Exception as exc:
        raise engine_failure() from exc


@dataclass(frozen=True)
class FlashCheck:
    failure: LivenessFailure | None = None
    captures: tuple[FlashCapture, ...] = ()


def _flash_captures(
    pipeline: FacePipeline, images: Sequence[bytes], face_policy: FacePolicy, *, enforce: bool
) -> tuple[FlashCapture, ...] | LivenessFailure | None:
    """Los fotogramas del destello analizados. Mientras solo se observa, uno que no sirve (o una falla
    del motor) se registra y el intento sigue sin la medición (None); obligatorio, es un fallo."""
    try:
        return tuple(pipeline.analyze_flash(image, policy=face_policy) for image in images)
    except FaceValidationError as exc:
        if enforce and exc.code in SECURITY_REASONS:
            raise SuspiciousCapture(exc.code, exc.details) from exc
        if enforce:
            return LivenessFailure("LIVENESS_FAILED", capture_error=exc)
        logger.info("Destello (observación): captura no medible (%s)", exc.code)
    except (cv2.error, ValueError) as exc:
        if enforce:
            return LivenessFailure("LIVENESS_FAILED", capture_error=FaceValidationError("INVALID_IMAGE"))
        logger.info("Destello (observación): captura ilegible (%s)", exc)
    except Exception as exc:
        if enforce:
            raise engine_failure() from exc
        logger.exception("El motor falló al medir el destello (observación): el intento sigue sin medirlo")
    return None


def check_flash(
    pipeline: FacePipeline,
    challenge: Challenge,
    images: Sequence[bytes],
    policy: PolicySnapshot,
    face_policy: FacePolicy,
    limits: SecurityThresholds,
) -> FlashCheck:
    """Reto fotométrico: el rostro debe reflejar los colores que pintó la pantalla, en orden.

    Siempre se mide (y se guarda en las métricas); solo con el destello obligatorio decide:
    - Sin respuesta medible (mucha luz ambiente): fallo de la prueba de vida FLASH_INCONCLUSIVE.
    - Medible pero sin seguir los colores: intento de engaño FLASH_MISMATCH (video inyectado o
      generado, que no ve la pantalla).
    """
    if not challenge.flash or not images:
        return FlashCheck()
    enforce = policy.flash_liveness == FlashMode.ENFORCE
    captures = _flash_captures(pipeline, images, face_policy, enforce=enforce)
    if isinstance(captures, LivenessFailure):
        return FlashCheck(failure=captures)
    if captures is None:
        return FlashCheck()
    response: FlashResponse = flash_response([c.sample for c in captures], challenge.flash)
    face_signals.current().flash = response
    if not enforce:
        return FlashCheck()
    if not response.conclusive(settings.FACE_FLASH_MIN_MAGNITUDE):
        return FlashCheck(failure=LivenessFailure("FLASH_INCONCLUSIVE"))
    if response.score < limits.flash_min_score:
        raise SuspiciousCapture("FLASH_MISMATCH", {"score": response.score, "required": limits.flash_min_score})
    return FlashCheck(captures=captures)


def check_liveness(
    pipeline: FacePipeline,
    challenge: Challenge | None,
    response: LivenessResponse,
    frontal: Sequence[FaceAnalysis],
    policy: PolicySnapshot,
    face_policy: FacePolicy,
    limits: SecurityThresholds,
) -> LivenessCheck:
    """Las capturas del reto deben llegar a tiempo humano, reflejar el destello (si se exige), mostrar
    cada movimiento pedido en orden, no ser una pantalla y ser la misma persona de las frontales.

    Lanza SuspiciousCapture si llegaron demasiado rápido, una imagen no es de la cámara o el rostro no
    siguió los colores; lo demás (sin movimiento, otra persona) es un fallo de la prueba de vida que se
    registra como tal.
    """
    if challenge is None or not response.steps:
        return LivenessCheck()
    ensure_human_timing(challenge, policy, len(response.flash))
    flash = check_flash(pipeline, challenge, response.flash, policy, face_policy, limits)
    if flash.failure is not None:
        return LivenessCheck(flash.failure, actions=challenge.actions)
    target = step_target(frontal, limits)
    signals = face_signals.current()
    turns: list[FaceAnalysis] = []
    for action, image in zip(challenge.actions, response.steps, strict=True):
        step = _analyze_step(pipeline, image, action, target, face_policy)
        if isinstance(step, LivenessFailure):
            return LivenessCheck(step, tuple(turns), actions=challenge.actions)
        signals.add_step(action, step)
        turns.append(step)
        consistency = max(cosine_similarity(step.embedding, f.embedding) for f in frontal)
        if consistency < settings.FACE_LIVENESS_CONSISTENCY_THRESHOLD:
            failure = LivenessFailure("LIVENESS_MISMATCH", round(consistency, 4))
            return LivenessCheck(failure, tuple(turns), actions=challenge.actions)
    return LivenessCheck(
        turns=tuple(turns),
        spoofed=spoofed(frontal, turns, face_policy),
        actions=challenge.actions,
        flash=flash.captures,
    )


def take_challenge(
    db: Session,
    actor_id: int,
    response: LivenessResponse,
    camera_label: str | None,
    policy: PolicySnapshot,
) -> Challenge | None:
    """Primer candado de toda captura facial, ANTES de analizarla: cámara real y el reto de quien
    opera la cámara. El reto se consume aunque la captura falle después: sirve una sola vez.

    Empieza también las mediciones del intento (face_signals), que se guardan con la bitácora."""
    signals = face_signals.begin(policy.flash_liveness if policy.liveness_required else None)
    ensure_real_camera(camera_label, policy)
    challenge = challenge_store.require(
        db,
        actor_id,
        response,
        required=policy.liveness_required,
        flash_required=policy.flash_liveness == FlashMode.ENFORCE,
    )
    if challenge is not None:
        signals.challenged(len(challenge.actions), challenge.issued_at, datetime.now(UTC))
    return challenge


def confirm_live(
    db: Session,
    company_id: int,
    pipeline: FacePipeline,
    challenge: tuple[Challenge | None, LivenessResponse],
    frontal: Sequence[FaceAnalysis],
    policy: PolicySnapshot,
    face_policy: FacePolicy,
    *,
    block_spoof: bool = True,
    flagged_spoof: bool = False,
) -> LivenessCheck:
    """Último candado, con las frontales ya analizadas: prueba de vida, suplantación y toma única.

    `block_spoof=False` (autoregistro) deja la sospecha de suplantación para el revisor (va en
    `LivenessCheck.spoofed`); `flagged_spoof` = el análisis de las frontales ya la había marcado.
    """
    issued, response = challenge
    face_signals.current().frontal = list(frontal)
    liveness = check_liveness(pipeline, issued, response, frontal, policy, face_policy, thresholds(db))
    if block_spoof and (flagged_spoof or liveness.spoofed):
        raise SuspiciousCapture("SPOOF_DETECTED")
    inspect_take(db, company_id, frontal, liveness.turns, policy, actions=liveness.actions, flash=liveness.flash)
    return liveness


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
    ) -> VerificationLog:
        signals = face_signals.finish()
        if signals is not None:  # intento facial: sus números van con él (misma transacción)
            FaceSecurityRepository(self.db).add_metric(signals.metric(company_id, success=success, reason=reason))
        log = self.logs.add(
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
        return log
