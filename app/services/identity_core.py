"""Piezas comunes de la identificación por rostro y por QR.

- VerificationService (EMPLOYEE): verificación 1:1, el empleado contra su propio rostro o su QR.
- CheckpointService (VALIDATOR): identifica a cualquier empleado de su empresa (rostro 1:N o QR).

Ambos usan las mismas reglas (umbral de confianza de la empresa, prueba de vida, mensajes) y
registran cada intento en la bitácora (attendance.verification_logs). Los mensajes de rechazo y las
instrucciones de la prueba de vida salen de los catálogos de la base (catalog_service).
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean

import cv2
import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import encrypt_bytes
from app.core.devices import platform_of
from app.core.exceptions import UnprocessableError
from app.core.observability import observed
from app.facial_recognition import (
    FaceAnalysis,
    FacePipeline,
    FacePolicy,
    FaceValidationError,
    LivenessAction,
    StepTarget,
)
from app.facial_recognition.calibration import match_confidence, model_floor_confidence, similarity_for_confidence
from app.facial_recognition.jpeg_tables import encoder_quality
from app.facial_recognition.matcher import (
    MatchRequirement,
    acceptance,
    cosine_similarity,
    embedding_to_bytes,
    similarity_matrix,
)
from app.facial_recognition.photometry import FlashResponse, flash_hex, flash_response
from app.facial_recognition.pipeline import FlashCapture
from app.i18n import LazyText, Text
from app.models import (
    CaptureTrace,
    Employee,
    EmployeeDeviceMode,
    FlashMode,
    RiskAction,
    SignalMode,
    VerificationLog,
    VerificationMethod,
)
from app.repositories.face_security_repository import FaceSecurityRepository
from app.repositories.risk_repository import CaptureTraceRepository, RiskAssessmentRepository
from app.repositories.verification_repository import VerificationLogRepository
from app.schemas.auth import DeviceLocation
from app.schemas.avatar import employee_avatar
from app.schemas.verification import FaceChallengeResponse, FlashPace, VerificationResult
from app.services import employee_devices, face_signals, flash_pacing, fraud_cases
from app.services.capture_guard import ensure_human_timing, ensure_real_camera, inspect_take, spoofed
from app.services.capture_protocol import BurstMeasure, burst_spec, parallax_of
from app.services.catalog_service import face_error_text, get_catalogs, reason_text
from app.services.client_evidence import ClientEvidence
from app.services.device_service import issue_api_nonce, issue_nonce
from app.services.face_security import SecurityThresholds, thresholds, under_attack
from app.services.face_service import SECURITY_REASONS, Reference, SuspiciousCapture, engine_failure
from app.services.fraud_cases import OpenedCase
from app.services.liveness_service import (
    MAX_STEPS,
    ApiDevice,
    Challenge,
    ChallengeOwner,
    LivenessResponse,
    challenge_store,
    enrollment_actions,
    user_of,
)
from app.services.policy_service import PolicySnapshot
from app.services.risk_engine import RiskOutcome

logger = logging.getLogger(__name__)

#: Llaves del mensaje de una identificación exitosa (catálogo de mensajes; se traduce al armar el resultado).
FACE_SUCCESS = "IDENTIFICATION_SUCCESS"
QR_SUCCESS = "IDENTIFICATION_SUCCESS"
MAX_FRONTAL_FRAMES = 3
#: Motivo de la bitácora de un intento al que el motor de riesgo le pidió "un paso más" (no cuenta para el bloqueo).
STEP_UP_REASON = "STEP_UP_REQUIRED"
#: Tipos de fotograma de la evidencia de un caso (frontal, movimiento del reto, color del destello).
EVIDENCE_FRONTAL = "FRONTAL"
EVIDENCE_STEP = "STEP"
EVIDENCE_FLASH = "FLASH"
#: La hoja de la ráfaga (antifraude 2a): entra a la evidencia por el mismo camino (decisión D1), en su propio lugar.
EVIDENCE_BURST = "BURST"


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
    #: La mejor similitud de cada captura (el motor de riesgo mide la holgura sobre lo exigido).
    similarities: tuple[float, ...] = ()


def burst_rejects(references: Sequence[np.ndarray], requirement: MatchRequirement) -> bool:
    """Consenso de identidad de la ráfaga (decisión del dueño, 2026-10-06). SOLO ENDURECE: nunca acepta a nadie que la
    regla de siempre rechace; solo puede convertir una coincidencia en "Rostro no reconocido".

    Valor: la MEDIANA, sobre los mejores recortes quietos de la ráfaga (`BurstAnalysis.identity`), de la mejor similitud
    de la fusión de cada recorte contra las muestras de la persona (una matriz, `similarity_matrix`); queda en las
    métricas del intento (`burst_consensus`). Regla: medible (al menos `FACE_CONSENSUS_MIN_FRAMES` recortes), el
    consenso debe llegar a `requirement.fused - FACE_CONSENSUS_MARGIN`. Sin ráfaga, mal formada, sin medir o con pocos
    recortes con rostro: no se juzga (la decisión de siempre).

    Por qué el margen: un recorte de 160 px (rostro de ≈ 100 px, la resolución de LFW con que se calibraron los
    umbrales) es algo menos nítido que la frontal de 1280 px: medido con el motor real, el recorte se parece a la
    frontal 0.032 menos que otra frontal del mismo rostro (mediana; 0.043 en el peor de 30 pares; R&D, P7), así que
    0.10 deja más del doble de esa brecha y la regla solo dispara cuando el video en vivo NO es la persona registrada
    (frontales inyectadas o cambiadas, un fotograma "con suerte"). La mediana no se mueve por uno o dos recortes malos
    (parpadeo, desenfoque)."""
    signals = face_signals.current()
    burst = signals.burst
    frames = burst.analysis.identity if burst is not None and burst.analysis is not None else ()
    if len(frames) < settings.FACE_CONSENSUS_MIN_FRAMES:
        return False
    signals.consensus = round(float(np.median(similarity_matrix(frames, references).max(axis=1))), 4)
    floor = requirement.fused - settings.FACE_CONSENSUS_MARGIN
    if signals.consensus >= floor:
        return False
    logger.info(
        "Consenso de la ráfaga bajo el mínimo: %.4f < %.4f (%s recortes): Rostro no reconocido",
        signals.consensus,
        floor,
        len(frames),
    )
    return True


@observed("face.match_one")
def match_one(frontal: Sequence[FaceAnalysis], references: Sequence[Reference], policy: PolicySnapshot) -> OneToOne:
    """Comparación 1:1 (el empleado que se verifica, o el dueño del QR en QR + rostro) contra todas
    sus muestras: las del registro aprobado y las que aprendió del uso (face_learning).

    Coincide si CADA frontal alcanza lo que exige la empresa (con la fusión, también el piso de cada modelo) Y, si se
    pudo medir, el consenso de la ráfaga no la contradice (`burst_rejects`, solo endurece)."""
    requirement = required_match(policy)
    vectors = [r.vector for r in references]
    scores, accepted = acceptance([f.embedding for f in frontal], vectors, requirement)
    similarities = [round(float(s), 4) for s in scores.max(axis=1)]
    closest = references[int(np.argmax(scores.max(axis=0)))].id
    rejected = burst_rejects(vectors, requirement)
    matched = bool(accepted.any(axis=1).all()) and not rejected
    return OneToOne(matched, mean_confidence(similarities), closest, tuple(similarities))


def ensure_frame_count(images: Sequence[bytes]) -> None:
    if not 1 <= len(images) <= MAX_FRONTAL_FRAMES:
        raise UnprocessableError(code="INVALID_FRAME_COUNT", params={"min": 1, "max": MAX_FRONTAL_FRAMES})


def _device_nonce(owner: ChallengeOwner, policy: PolicySnapshot, *, device: bool) -> str | None:
    """El reto que firma la llave del dispositivo: SIEMPRE para un dispositivo de la API pública (su firma es
    obligatoria, `api_verification_service`); para el empleado que se captura a sí mismo, según el modo de su
    empresa."""
    if isinstance(owner, ApiDevice):
        return issue_api_nonce(owner.key_id, owner.device_hash)
    return issue_nonce(owner) if device and policy.employee_device_mode != EmployeeDeviceMode.OFF else None


def issue_challenge(
    db: Session,
    owner: ChallengeOwner,
    policy: PolicySnapshot,
    company_id: int,
    *,
    step_up: bool = False,
    device: bool = False,
    enrollment: bool = False,
) -> FaceChallengeResponse:
    """Reto de prueba de vida de uso único para quien lo pide (`owner`: una cuenta o un dispositivo de la API
    pública): los movimientos al azar (los que pide la
    empresa, o el máximo si está bajo ataque), los colores del destello y los umbrales vigentes (la app
    guía a la persona hasta ellos).

    `step_up` (riesgo medio del motor de riesgo, decisión D3): el reto de "un paso más": el máximo de movimientos
    y el destello OBLIGATORIO para ese intento aunque la empresa solo lo mida.

    `enrollment` (decisión del dueño, 2026-10-07): el reto del REGISTRO facial pide SIEMPRE los cuatro movimientos
    (`enrollment_actions`: derecha, izquierda, arriba y abajo, en orden al azar), sin importar `liveness_steps` ni el
    refuerzo por ataques; el registro rechaza cualquier otro reto (`is_enrollment_challenge`).

    `device` (el empleado se captura a sí mismo, decisión D2): con el reto va otro que firma la llave de su
    dispositivo (`device_nonce`, el mismo de los validadores: ligado a la cuenta y con vencimiento), también sin prueba
    de vida; con el modo apagado no se pide nada. Un dispositivo de la API lo recibe siempre (`_device_nonce`).

    La API pública nunca usa el destello (retirado de la experiencia, migración 0080, y sin canal en vivo para
    dictarlo): su reto va sin colores aunque una empresa lo tuviera encendido.

    Antifraude 2a: con `flash_paced` los colores NO viajan con el reto (los dicta el servidor uno por uno por el canal
    en vivo, `flash_pacing`; aquí va su token inicial) y con `capture_burst` se pide la ráfaga de recortes."""
    nonce = _device_nonce(owner, policy, device=device)
    if not policy.liveness_required:
        return FaceChallengeResponse(liveness_required=False, device_nonce=nonce)
    reinforced = under_attack(db, company_id, datetime.now(UTC))
    steps = MAX_STEPS if reinforced or step_up else policy.liveness_steps
    # Decisión del dueño (2026-10-06): OFF es OFF también en «un paso más» (el destello se retiró de la experiencia).
    api = isinstance(owner, ApiDevice)
    flash = settings.FACE_FLASH_COLORS if policy.flash_liveness != FlashMode.OFF and not api else 0
    challenge = challenge_store.issue(
        db,
        owner,
        steps=steps,
        lifetime_seconds=policy.liveness_timeout_seconds,
        flash=flash,
        step_up=step_up,
        reinforced=reinforced,
        paced=policy.flash_paced,
        actions=enrollment_actions() if enrollment else None,
    )
    pace = (
        FlashPace(
            token=flash_pacing.start_token(challenge),
            total=len(challenge.flash),
            window_ms=settings.FACE_FLASH_PACE_WINDOW_MS,
        )
        if challenge.flash_paced
        else None
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
        flash=[] if pace is not None else [flash_hex(code) for code in challenge.flash],
        flash_required=bool(challenge.flash) and policy.flash_liveness == FlashMode.ENFORCE,
        expires_in=policy.liveness_timeout_seconds,
        step_up=step_up,
        device_nonce=nonce,
        flash_pace=pace,
        burst=burst_spec() if policy.capture_burst else None,
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
    def text(self) -> LazyText:
        """Para la identificación (diferido): el motivo de la bitácora (o el error de la captura del reto)."""
        if self.capture_error is not None:
            return face_error_text(self.capture_error.code, self.capture_error.details)
        return reason_text(self.reason)


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


@observed("liveness.flash")
def check_flash(
    pipeline: FacePipeline,
    challenge: Challenge,
    images: Sequence[bytes],
    policy: PolicySnapshot,
    face_policy: FacePolicy,
    limits: SecurityThresholds,
) -> FlashCheck:
    """Reto fotométrico: el rostro debe reflejar los colores que pintó la pantalla, en orden.

    Siempre se mide (y se guarda en las métricas); solo con el destello obligatorio (o en un reto de "un paso
    más") decide:
    - Sin respuesta medible (mucha luz ambiente): fallo de la prueba de vida FLASH_INCONCLUSIVE.
    - Medible pero sin seguir los colores: intento de engaño FLASH_MISMATCH (video inyectado o
      generado, que no ve la pantalla).
    - Siguió los colores, pero el fondo respondió igual que el rostro (cociente rostro/fondo bajo el mínimo
      autocalibrado): intento de engaño FLASH_FLAT (pantalla o papel frente a la cámara, prototipo P1).

    Con el destello dictado por el servidor y su comprobante válido, los colores son los que de verdad se revelaron
    (`flash_pacing.verify`, anotado por `take_challenge`); sin él, los del respaldo del reto.
    """
    if not challenge.flash or not images:
        return FlashCheck()
    enforce = policy.flash_liveness == FlashMode.ENFORCE
    captures = _flash_captures(pipeline, images, face_policy, enforce=enforce)
    if isinstance(captures, LivenessFailure):
        return FlashCheck(failure=captures)
    if captures is None:
        return FlashCheck()
    pace = face_signals.current().pace
    colors = pace.colors if pace is not None and pace.colors else challenge.flash
    response: FlashResponse = flash_response([c.sample for c in captures], colors)
    face_signals.current().flash = response
    if not enforce:
        return FlashCheck()
    if not response.conclusive(settings.FACE_FLASH_MIN_MAGNITUDE):
        return FlashCheck(failure=LivenessFailure("FLASH_INCONCLUSIVE"))
    if response.score < limits.flash_min_score:
        raise SuspiciousCapture("FLASH_MISMATCH", {"score": response.score, "required": limits.flash_min_score})
    ratio = response.face_ratio
    if ratio is not None and ratio < limits.flash_min_ratio:
        # El rostro y el fondo respondieron igual: una pantalla o un papel frente a la cámara (P1). Sin fondo
        # medible (rostro que llena el encuadre) decide solo la respuesta del rostro.
        raise SuspiciousCapture("FLASH_FLAT", {"ratio": ratio, "required": limits.flash_min_ratio})
    return FlashCheck(captures=captures)


@observed("liveness.check")
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
    # Antifraude 2a: la ráfaga empieza YA en otro worker libre (otro núcleo, sin transacción abierta) y se recoge al
    # final; sin uno libre se mide al final con el mismo worker, como antes (decisión del dueño, 2026-10-06).
    burst = BurstMeasure(pipeline, response, frontal) if policy.capture_burst else None
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
    # Antifraude 2a: el paralaje de los giros (puntos que ya se tienen) y la ráfaga (ya medida en paralelo, o ahora con
    # el mismo worker); solo números para el motor de riesgo.
    signals.parallax = parallax_of(frontal, turns, challenge.actions)
    if burst is not None:
        signals.burst = burst.result()
    return LivenessCheck(
        turns=tuple(turns),
        spoofed=spoofed(frontal, turns, face_policy),
        actions=challenge.actions,
        flash=flash.captures,
    )


def take_challenge(
    db: Session,
    actor: ChallengeOwner,
    response: LivenessResponse,
    camera_label: str | None,
    policy: PolicySnapshot,
    frontal: Sequence[bytes] = (),
    client: ClientEvidence | None = None,
) -> Challenge | None:
    """Primer candado de toda captura facial, ANTES de analizarla: cámara real y el reto de quien
    opera la cámara. El reto se consume aunque la captura falle después: sirve una sola vez.

    Empieza también las mediciones del intento (face_signals), que se guardan con la bitácora.

    Termina la transacción de lo leído hasta aquí (política, bloqueo, muestras del empleado): el análisis que sigue
    es CPU de 0.3 a 3 s y, detrás de PgBouncer, una transacción abierta ocupa una conexión REAL del servidor todo
    ese tiempo. Nada de lo que se decide después depende de esas lecturas bajo candado: lo que se registra se
    escribe en su propia transacción (la bitácora y sus métricas juntas) y la asistencia vuelve a leer y bloquear
    al empleado antes de escribir.

    Antifraude 1b: anota también lo que informó el cliente (`client`) y la calidad JPEG de cada captura (solo sus
    encabezados: microsegundos)."""
    signals = face_signals.begin(policy.flash_liveness if policy.liveness_required else None)
    signals.camera, signals.camera_required = camera_label, policy.block_virtual_cameras
    signals.actor_id, signals.client = user_of(actor), client
    signals.jpeg = tuple(encoder_quality(image) for image in (*frontal, *response.steps, *response.flash))
    # Evidencia por si el intento abre un caso (decisión D1): solo en memoria durante la petición.
    signals.keep_evidence = policy.fraud_evidence and settings.FRAUD_EVIDENCE_FRAMES_PER_ATTEMPT > 0
    limit = settings.FRAUD_EVIDENCE_FRAMES_PER_ATTEMPT
    signals.keep(EVIDENCE_FRONTAL, frontal, limit)
    signals.keep(EVIDENCE_STEP, response.steps, limit)
    signals.keep(EVIDENCE_FLASH, response.flash, limit)
    # La hoja de la ráfaga, en un lugar propio (una sola imagen): solo viaja al bucket si el intento abre un caso.
    signals.keep(EVIDENCE_BURST, (response.burst,) if response.burst else (), limit + 1)
    ensure_real_camera(camera_label, policy)
    challenge = challenge_store.require(
        db,
        actor,
        response,
        required=policy.liveness_required,
        flash_required=policy.flash_liveness == FlashMode.ENFORCE,
    )
    db.commit()
    if challenge is not None:
        signals.challenged(len(challenge.actions), challenge.issued_at, datetime.now(UTC))
        signals.step_up, signals.reinforced = challenge.step_up, challenge.reinforced
        if challenge.flash_paced and response.flash and isinstance(actor, int):
            # El destello dictado: ¿cada captura es la que se comprometió a tiempo? (sin consultas; solo su firma). Solo
            # una cuenta lo recibe: el reto de la API nunca se dicta.
            signals.pace = flash_pacing.verify(response.flash_receipt, challenge, actor, response.flash)
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
    others: Sequence[FaceAnalysis] = (),
) -> LivenessCheck:
    """Último candado, con las frontales ya analizadas: prueba de vida, suplantación y toma única.

    `block_spoof=False` (autoregistro) deja la sospecha de suplantación para el revisor (va en
    `LivenessCheck.spoofed`); `flagged_spoof` = el análisis de las frontales ya la había marcado.
    `others`: capturas válidas de la toma que no quedaron como frontales (las fotos útiles del registro que no se
    eligieron como referencia): solo cuentan para el reenvío (`inspect_take`).
    """
    issued, response = challenge
    face_signals.current().frontal = list(frontal)
    limits = thresholds(db)
    db.commit()  # los umbrales pudieron leerse de la BD: nada abierto mientras se analizan los pasos (CPU)
    liveness = check_liveness(pipeline, issued, response, frontal, policy, face_policy, limits)
    if block_spoof and (flagged_spoof or liveness.spoofed):
        raise SuspiciousCapture("SPOOF_DETECTED")
    inspect_take(
        db,
        company_id,
        frontal,
        liveness.turns,
        policy,
        actions=liveness.actions,
        flash=liveness.flash,
        others=others,
    )
    return liveness


def failed(method: VerificationMethod, text: LazyText) -> VerificationResult:
    """Una identificación que no pasó, con su motivo para la persona (diferido: el sobre lo arma en cada idioma)."""
    return VerificationResult(verified=False, method=method, message=text)


def succeeded(
    employee: Employee, method: VerificationMethod, key: str, *, confidence: float | None
) -> VerificationResult:
    """El resultado de una identificación exitosa; `key`, la llave de su mensaje (en el idioma de la petición)."""
    return VerificationResult(
        verified=True,
        method=method,
        message=Text(key),
        employee_id=employee.id,
        employee_number=employee.employee_number,
        name=employee.full_name,
        avatar=employee_avatar(employee),
        confidence=None if confidence is None else round(max(0.0, min(1.0, confidence)), 7),
        verified_at=datetime.now(UTC),
    )


class StepUpRequired(UnprocessableError):
    """422 STEP_UP_REQUIRED: el motor de riesgo pide "un paso más" (riesgo medio, decisión D3). Trae el reto nuevo
    (`details.challenge`: el máximo de movimientos y el destello obligatorio) para que la app lo complete SIN
    repetir el escaneo; no cuenta para el bloqueo temporal."""

    def __init__(self, challenge: FaceChallengeResponse) -> None:
        super().__init__(
            face_error_text("STEP_UP_REQUIRED"),
            code="STEP_UP_REQUIRED",
            details={"challenge": challenge.model_dump(mode="json")},
        )
        #: El reto nuevo (la API pública lo entrega dentro de su resultado, no como error).
        self.challenge = challenge


def enforce_risk(
    db: Session,
    outcome: RiskOutcome,
    *,
    actor_id: ChallengeOwner,
    policy: PolicySnapshot,
    company_id: int,
    record: Callable[[str], None],
    device: bool = False,
) -> None:
    """Lo que el motor de riesgo NO deja pasar: negar (regla dura o puntaje crítico: intento sospechoso que cuenta
    para el bloqueo) o pedir un paso más (con el reto nuevo; `device`: con el reto del dispositivo del empleado).
    `record` anota el intento fallido en la bitácora con su motivo (y con él la decisión, sus métricas y su caso).
    Permitir, avisar o "en revisión" siguen su camino."""
    if outcome.action == RiskAction.DENY:
        record(outcome.deny_reason)
        raise SuspiciousCapture(outcome.deny_reason)
    if outcome.action == RiskAction.STEP_UP:
        record(STEP_UP_REASON)
        raise StepUpRequired(issue_challenge(db, actor_id, policy, company_id, step_up=True, device=device))


def ensure_verification_location(policy: PolicySnapshot, location: DeviceLocation | None) -> None:
    """Exige la ubicación de una verificación cuando la empresa la puso en «Obligatoria»
    (`verification_location = ENFORCE`; decisión del dueño, 2026-10-07): sin ella, 422 `LOCATION_REQUIRED`; con ella
    pero sin precisión o peor que `max_location_accuracy_m` de la empresa, 422 `LOCATION_INVALID`. Con OBSERVE u OFF
    nunca bloquea (la ubicación solo se registra para el mapa). Se llama ANTES del motor y del reto (como la presencia
    del validador): nada se consume ni cuenta como intento si falta la ubicación. No hay geocerca aquí (a diferencia del
    validador, que la tiene): una verificación puede ocurrir en cualquier lugar; solo se exige que llegue y sea precisa.
    La asistencia NO pasa por aquí (su ubicación vive en `attendance_events`): quien la verifica no la exige."""
    if policy.verification_location != SignalMode.ENFORCE:
        return
    # El `code` es estable (lo que distingue la app y los SDK, contrato `docs/sdk/contrato-verificacion.md`); el `key`
    # da el texto propio de la verificación (el de `LOCATION_REQUIRED` del catálogo es del inicio de sesión
    # del validador).
    if location is None:
        raise UnprocessableError(code="LOCATION_REQUIRED", key="VERIFICATION_LOCATION_REQUIRED")
    if location.accuracy is None or location.accuracy > policy.max_location_accuracy_m:
        raise UnprocessableError(code="LOCATION_INVALID", key="VERIFICATION_LOCATION_INVALID")


class IdentityLog:
    """Bitácora de intentos de identificación (quién lo hizo, a quién, cómo y con qué resultado)."""

    def __init__(self, db: Session, *, ip: str | None, user_agent: str | None) -> None:
        self.db = db
        self.logs = VerificationLogRepository(db)
        self.ip = ip
        self.user_agent = user_agent
        #: Dónde se hizo la verificación (decisión del dueño, 2026-10-07): se fija una vez por petición antes de
        #: registrar y queda en las columnas del log para el mapa de «Verificaciones». None cuando no llevó ubicación.
        self.location: DeviceLocation | None = None

    def record(
        self,
        *,
        company_id: int,
        employee_id: int | None,
        actor_id: int | None,
        method: VerificationMethod,
        success: bool,
        score: float | None = None,
        reason: str | None = None,
        device_hash: str | None = None,
    ) -> VerificationLog:
        """El intento en la bitácora y, si fue facial, en la MISMA transacción: sus números (enlazados con él), la
        decisión del motor de riesgo, las huellas perceptuales de sus capturas (si coincidió) y su caso de fraude (si
        fue sospechoso o de riesgo alto). La evidencia del caso se sube después de confirmar (sin transacción
        abierta, de mejor esfuerzo)."""
        signals = face_signals.finish()
        now = datetime.now(UTC)
        location = self.location
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
                device_hash=device_hash,
                # Dónde se hizo (si la verificación llevó ubicación): la empresa la ve en el mapa de «Verificaciones».
                latitude=location.latitude if location else None,
                longitude=location.longitude if location else None,
                location_accuracy_m=round(location.accuracy) if location and location.accuracy is not None else None,
                created_at=now,
            )
        )
        case = self._facial(signals, log, now) if signals is not None else None
        self.db.commit()
        if case is not None and signals is not None and signals.evidence:
            fraud_cases.store_evidence(self.db, case, signals.evidence, log.id)
        return log

    def _facial(self, signals: face_signals.AttemptSignals, log: VerificationLog, now: datetime) -> OpenedCase | None:
        metric = signals.metric(log.company_id, success=log.success, reason=log.reason, log_id=log.id)
        metric.created_at = now
        # La plataforma del navegador (categoría gruesa, nunca el User-Agent): agrupa la deriva de las señales.
        metric.platform = platform_of(self.user_agent)
        FaceSecurityRepository(self.db).add_metric(metric)
        if signals.assessment is not None:
            signals.assessment.verification_log_id = log.id
            signals.assessment.created_at = now
            RiskAssessmentRepository(self.db).add(signals.assessment)
        if signals.device is not None and log.employee_id is not None:
            # El dispositivo del empleado (decisión D2): uso nuevo o último uso; superar aquí un paso más lo vuelve de
            # confianza (modo «Un paso más en un dispositivo nuevo»).
            stepped_up = log.success and signals.step_up
            employee_devices.remember(self.db, log.company_id, log.employee_id, signals.device, stepped_up=stepped_up)
        if log.success and log.employee_id is not None:
            # Lo que coincidió es la referencia contra la que se reconocerá su reenvío modificado.
            CaptureTraceRepository(self.db, log.company_id).add_all(
                CaptureTrace(
                    company_id=log.company_id,
                    employee_id=log.employee_id,
                    created_at=now,
                    face_phash=capture.face_phash,
                    frame_phash=capture.frame_phash,
                    embedding_encrypted=encrypt_bytes(embedding_to_bytes(capture.embedding)),
                    dimension=int(capture.embedding.shape[0]),
                )
                for capture in signals.frontal
                if capture.face_phash is not None and capture.frame_phash is not None
            )
        self.db.flush()
        return fraud_cases.observe(self.db, log=log, signals=signals, metric=metric, camera=signals.camera)
