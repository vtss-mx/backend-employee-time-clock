"""Verificación de identidad 1:1 (rostro o QR).

- El EMPLOYEE autenticado se verifica a sí mismo: la evidencia (rostro o QR) debe corresponder a
  la cuenta que inició sesión.
- La COMPANY verifica en persona a uno de sus empleados: su rostro contra el registro aprobado.

Cada intento queda en `verification_logs` (con quién lo hizo). Las reglas comunes viven en
identity_core.
"""

import logging

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.facial_recognition import FacePipeline
from app.models import Employee, FaceStatus, User, VerificationLog, VerificationMethod
from app.schemas.verification import VerificationResult
from app.services.attempt_guard import ensure_unlocked
from app.services.catalog_service import get_catalogs
from app.services.employee_access import approved_employee
from app.services.face_learning import Evidence, FaceLearning
from app.services.face_service import FaceService, SuspiciousCapture
from app.services.identity_core import (
    FACE_SUCCESS,
    IdentityLog,
    confirm_live,
    ensure_frame_count,
    failed,
    match_one,
    reason_message,
    succeeded,
    take_challenge,
)
from app.services.liveness_service import NO_RESPONSE, LivenessResponse
from app.services.policy_service import PolicyService

logger = logging.getLogger(__name__)


class VerificationService:
    def __init__(self, db: Session, *, ip: str | None = None, user_agent: str | None = None) -> None:
        self.db = db
        self.log = IdentityLog(db, ip=ip, user_agent=user_agent)
        #: El último intento registrado (la asistencia lo enlaza como evidencia del registro).
        self.last_log: VerificationLog | None = None

    def verify_face(
        self,
        user: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        *,
        liveness: LivenessResponse = NO_RESPONSE,
        camera_label: str | None = None,
    ) -> VerificationResult:
        """Verificación 1:1 del empleado autenticado.

        1. Reto de prueba de vida válido, de uso único y del mismo usuario.
        2. Cada frame frontal: un rostro, calidad, pose frontal y sin accesorios (422 si no).
        3. Respuesta al reto: el destello y cada movimiento pedido, de la misma persona.
        4. Cada frame frontal debe alcanzar la confianza de la empresa contra sus muestras.
        5. Si fue holgada y segura, la galería del empleado aprende de ella (face_learning).
        """
        return self.verify_employee_face(
            approved_employee(user),
            user,
            frontal_images,
            pipeline,
            liveness=liveness,
            camera_label=camera_label,
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
    ) -> VerificationResult:
        """La empresa verifica la identidad de un empleado presente: su rostro contra su registro aprobado."""
        if not employee.active:
            raise ConflictError("El empleado está inactivo", code="EMPLOYEE_INACTIVE")
        if employee.face_status != FaceStatus.APPROVED:
            raise ConflictError(
                "El empleado aún no tiene un rostro aprobado: regístralo primero", code="FACE_NOT_APPROVED"
            )
        return self.verify_employee_face(
            employee,
            operator,
            frontal_images,
            pipeline,
            liveness=liveness,
            camera_label=camera_label,
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

        try:
            challenge = take_challenge(self.db, actor.id, liveness, camera_label, policy)
            # Errores de calidad/accesorios -> 422 (no cuentan como intento fallido).
            face_policy = policy.face_policy(employee)
            frontal, _ = face_service.analyze_frames(frontal_images, policy=face_policy)
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
        # La galería del empleado evoluciona: suma utilidad a la muestra que decidió y, si la captura
        # es segura, aprende de ella (se guarda junto con el intento en la bitácora).
        evidence = Evidence(frontal, live=bool(check.turns))
        FaceLearning(face_service, policy).reinforce(employee, match.closest, evidence, references)
        self._record(employee, actor, VerificationMethod.FACE, True, match.score, None)
        return succeeded(employee, VerificationMethod.FACE, FACE_SUCCESS, confidence=match.score)

    # ---------- Internos ----------

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
