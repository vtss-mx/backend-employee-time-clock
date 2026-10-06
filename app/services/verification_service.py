"""Verificación de identidad 1:1 (rostro o QR).

- El EMPLOYEE autenticado se verifica a sí mismo: la evidencia (rostro o QR) debe corresponder a
  la cuenta que inició sesión.
- La COMPANY verifica en persona a uno de sus empleados: su rostro contra el registro aprobado.

Cada intento queda en `verification_logs` (con quién lo hizo). Las reglas comunes viven en
identity_core.
"""

import logging

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError
from app.core.ip_intel import IpInfo
from app.facial_recognition import FacePipeline
from app.models import Employee, FaceStatus, RiskSignal, User, VerificationLog, VerificationMethod
from app.schemas.verification import VerificationResult
from app.services import face_signals
from app.services.attempt_guard import ensure_unlocked
from app.services.catalog_service import get_catalogs
from app.services.client_evidence import ClientEvidence
from app.services.employee_access import approved_employee
from app.services.face_gallery import Gallery, face_galleries, rival_of
from app.services.face_learning import Evidence, FaceLearning
from app.services.face_service import FaceService, SuspiciousCapture
from app.services.identity_core import (
    FACE_SUCCESS,
    IdentityLog,
    confirm_live,
    enforce_risk,
    ensure_frame_count,
    failed,
    match_one,
    reason_message,
    required_match,
    succeeded,
    take_challenge,
)
from app.services.liveness_service import NO_RESPONSE, LivenessResponse
from app.services.policy_service import PolicyService, PolicySnapshot
from app.services.risk_engine import Match, RiskEngine, measures

logger = logging.getLogger(__name__)


class VerificationService:
    def __init__(self, db: Session, *, ip: str | None = None, user_agent: str | None = None) -> None:
        self.db = db
        self.log = IdentityLog(db, ip=ip, user_agent=user_agent)
        #: El último intento registrado (la asistencia lo enlaza como evidencia del registro).
        self.last_log: VerificationLog | None = None
        #: Motivos de negocio (catalog.review_reasons) si el último intento quedó "en revisión".
        self.last_review: tuple[str, ...] = ()
        #: La red de la IP del último intento (la asistencia la guarda con el registro: NETWORK_JUMP del siguiente).
        self.last_network: IpInfo | None = None

    def verify_face(
        self,
        user: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        *,
        liveness: LivenessResponse = NO_RESPONSE,
        camera_label: str | None = None,
        client: ClientEvidence | None = None,
    ) -> VerificationResult:
        """Verificación 1:1 del empleado autenticado (`client`: lo que informó su app, con la prueba de su
        dispositivo).

        1. Reto de prueba de vida válido, de uso único y del mismo usuario.
        2. Cada frame frontal: un rostro, calidad, pose frontal y sin accesorios (422 si no).
        3. Respuesta al reto: el destello y cada movimiento pedido, de la misma persona.
        4. Cada frame frontal debe alcanzar la confianza de la empresa contra sus muestras.
        5. El motor de riesgo decide (permitir, avisar, "en revisión", un paso más o negar).
        6. Si fue holgada, segura y sin riesgo, la galería del empleado aprende de ella (face_learning).
        """
        return self.verify_employee_face(
            approved_employee(user),
            user,
            frontal_images,
            pipeline,
            liveness=liveness,
            camera_label=camera_label,
            client=client,
        )

    def verify_in_person(
        self,
        employee: Employee,
        operator: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        *,
        liveness: LivenessResponse = NO_RESPONSE,
        camera_label: str | None = None,
        client: ClientEvidence | None = None,
    ) -> VerificationResult:
        """La empresa verifica la identidad de un empleado presente: su rostro contra su registro aprobado (`client`:
        sin la prueba del dispositivo, que es de la empresa)."""
        if not employee.active:
            raise ConflictError(code="EMPLOYEE_INACTIVE")
        if employee.face_status != FaceStatus.APPROVED:
            raise ConflictError(code="FACE_NOT_APPROVED", key="EMPLOYEE_FACE_NOT_APPROVED")
        return self.verify_employee_face(
            employee,
            operator,
            frontal_images,
            pipeline,
            liveness=liveness,
            camera_label=camera_label,
            client=client,
        )

    def verify_employee_face(
        self,
        employee: Employee,
        actor: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        *,
        liveness: LivenessResponse,
        camera_label: str | None = None,
        client: ClientEvidence | None = None,
    ) -> VerificationResult:
        """Rostro de `employee` contra sus muestras; `actor` opera la cámara (su reto y su bitácora).

        Demasiados fallos seguidos del empleado bloquean temporalmente (429 FACE_LOCKED). Un intento
        sospechoso (pantalla, foto fija, reenvío, cámara virtual...) se registra y responde 422.
        """
        ensure_frame_count(frontal_images)
        policy = PolicyService(self.db, employee.company_id).current()
        ensure_unlocked(self.db, policy, employee_id=employee.id)
        face_service = FaceService(self.db, pipeline)
        references = face_service.references_for(employee)
        if not references:
            raise ConflictError(get_catalogs().face_error_message("FACE_NOT_REGISTERED"), code="FACE_NOT_REGISTERED")
        # 1:N en cada 1:1: la galería se lee ahora, en la transacción de lo leído que `take_challenge` cierra.
        gallery = self._rival_gallery(employee.company_id, pipeline, policy)

        try:
            challenge = take_challenge(self.db, actor.id, liveness, camera_label, policy, frontal_images, client)
            # Errores de calidad/accesorios -> 422 (no cuentan como intento fallido).
            face_policy = policy.face_policy(employee)
            frontal, _ = face_service.analyze_frames(frontal_images, policy=face_policy)
            # Solo CPU y sin transacción abierta: ¿a qué OTRO empleado se parece más este rostro?
            face_signals.current().rival = rival_of(gallery, [f.embedding for f in frontal], exclude=employee.id)
            check = confirm_live(
                self.db, employee.company_id, pipeline, (challenge, liveness), frontal, policy, face_policy
            )
        except SuspiciousCapture as exc:
            logger.warning("Intento sospechoso (empleado %s, operador %s): %s", employee.id, actor.id, exc.code)
            self._record(employee, actor, VerificationMethod.FACE, False, None, exc.code)
            raise

        failure = check.failure
        if failure is not None:
            logger.info("Prueba de vida no superada (empleado %s): %s", employee.id, failure.reason)
            self._record(employee, actor, VerificationMethod.FACE, False, failure.score, failure.reason)
            return failed(VerificationMethod.FACE, failure.message)

        match = match_one(frontal, references, policy)
        if not match.matched:
            self._record(employee, actor, VerificationMethod.FACE, False, match.score, "NO_MATCH")
            return failed(VerificationMethod.FACE, reason_message("NO_MATCH"))
        # El motor de riesgo (después del candado de identidad): permitir, avisar, "en revisión", un paso más o negar.
        outcome = RiskEngine(self.db, employee.company_id, policy).evaluate(
            employee_id=employee.id,
            frontal=frontal,
            match=Match(match.similarities, required_match(policy).fused),
            can_step_up=policy.liveness_required,
        )
        enforce_risk(
            self.db,
            outcome,
            actor_id=actor.id,
            policy=policy,
            company_id=employee.company_id,
            record=lambda reason: self._record(employee, actor, VerificationMethod.FACE, False, match.score, reason),
            device=client is not None and client.device is not None,
        )
        self.last_review = face_signals.current().review_reasons
        self.last_network = face_signals.current().network
        if outcome.clean:
            # La galería del empleado evoluciona: suma utilidad a la muestra que decidió y, si la captura es segura,
            # aprende de ella. Nunca de un intento con riesgo (no aprende "rostros en pantalla").
            evidence = Evidence(frontal, live=bool(check.turns))
            FaceLearning(face_service, policy).reinforce(employee, match.closest, evidence, references)
        self._record(employee, actor, VerificationMethod.FACE, True, match.score, None)
        result = succeeded(employee, VerificationMethod.FACE, FACE_SUCCESS, confidence=match.score)
        result.review = outcome.review
        return result

    # ---------- Internos ----------

    def _rival_gallery(self, company_id: int, pipeline: FacePipeline, policy: PolicySnapshot) -> Gallery | None:
        """La galería de la empresa para el 1:N en cada 1:1 (solo si su señal se mide): la de memoria si es reciente;
        nunca una que no cabe en la caché (`face_gallery.recent`)."""
        if not measures(policy, RiskSignal.IDENTITY_MISMATCH):
            return None
        max_age = settings.RISK_IDENTITY_GALLERY_MAX_AGE_SECONDS
        return face_galleries.recent(self.db, company_id, pipeline.model_name, max_age=max_age)

    def _record(
        self,
        employee: Employee,
        user: User,
        method: VerificationMethod,
        success: bool,
        score: float | None,
        reason: str | None,
    ) -> None:
        self.last_log = self.log.record(
            company_id=employee.company_id,
            employee_id=employee.id,
            actor_id=user.id,
            method=method,
            success=success,
            score=score,
            reason=reason,
        )
