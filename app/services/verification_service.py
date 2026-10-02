"""Verificación de identidad del EMPLOYEE autenticado (rostro o QR).

Ambos métodos son verificaciones 1:1: se comprueba que la evidencia presentada
(rostro o QR) corresponde a la cuenta que inició sesión. Cada intento queda
registrado en `verification_logs`. Las reglas comunes viven en identity_core.
"""

import logging

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_bytes
from app.core.exceptions import ConflictError, PermissionDeniedError
from app.facial_recognition import FacePipeline
from app.models import Employee, EnrollmentStatus, FaceStatus, User, VerificationMethod
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.schemas.verification import FaceChallengeResponse, VerificationResult
from app.services.face_service import FaceService
from app.services.identity_core import (
    FACE_FAILED,
    FACE_SUCCESS,
    QR_SUCCESS,
    IdentityLog,
    ensure_frame_count,
    failed,
    issue_challenge,
    liveness_failure,
    match_references,
    mean_confidence,
    qr_message,
    required_similarity,
    succeeded,
)
from app.services.liveness_service import challenge_store
from app.services.policy_service import PolicyService
from app.services.qr_service import QrService

logger = logging.getLogger(__name__)


class VerificationService:
    def __init__(self, db: Session, *, ip: str | None = None, user_agent: str | None = None) -> None:
        self.db = db
        self.log = IdentityLog(db, ip=ip, user_agent=user_agent)

    def issue_face_challenge(self, user: User) -> FaceChallengeResponse:
        """Se usa tanto en el registro facial como en la verificación."""
        if user.employee is None or not user.employee.active:
            raise PermissionDeniedError("Solo empleados activos")
        return issue_challenge(self.db, user.id, PolicyService(self.db, user.employee.company_id).current())

    def verify_face(
        self,
        user: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        *,
        challenge_id: str | None = None,
        challenge_image: bytes | None = None,
    ) -> VerificationResult:
        """Verificación 1:1 del empleado autenticado.

        1. Reto de prueba de vida válido, de uso único y del mismo usuario.
        2. Cada frame frontal: un rostro, calidad, pose frontal y sin accesorios (422 si no).
        3. Frame del reto: la cabeza girada en la dirección solicitada y misma persona.
        4. Cada frame frontal debe alcanzar la confianza de la empresa contra sus muestras.
        """
        employee = self._employee_of(user)
        ensure_frame_count(frontal_images)
        policy = PolicyService(self.db, employee.company_id).current()
        face_service = FaceService(self.db, pipeline)
        references = face_service.load_references(employee.id) or self.migrate_references(employee, face_service)
        if not references:
            raise ConflictError(
                "No tienes información facial registrada. Contacta a tu empresa.", code="FACE_NOT_REGISTERED"
            )

        challenge = challenge_store.require(
            self.db, user.id, challenge_id, challenge_image, required=policy.liveness_required
        )

        # Errores de calidad/accesorios/suplantación -> 422 (no cuentan como intento fallido).
        frontal, _ = face_service.analyze_frames(frontal_images, policy=policy.face_policy(employee))

        failure = liveness_failure(pipeline, challenge, challenge_image, frontal)
        if failure is not None:
            logger.info("Prueba de vida no superada (empleado %s): %s", employee.id, failure.reason)
            self._record(employee, user, VerificationMethod.FACE, False, failure.score, failure.reason)
            return failed(VerificationMethod.FACE, failure.message)

        required = required_similarity(policy)
        similarities = match_references(frontal, references, required)
        matched = min(similarities) >= required
        score = mean_confidence(similarities)

        self._record(employee, user, VerificationMethod.FACE, matched, score, None if matched else "NO_MATCH")
        if not matched:
            return failed(VerificationMethod.FACE, FACE_FAILED)
        if len(references) < settings.FACE_MAX_SAMPLES_PER_EMPLOYEE and challenge is not None:
            # Tras una migración de modelo el empleado tiene pocas muestras: se completan con
            # capturas ya verificadas (identidad + prueba de vida superadas), hasta el máximo.
            best = max(frontal, key=lambda a: a.quality_score)
            face_service.store(employee.id, [best], replace=False, active=True)
            self.db.commit()
        return succeeded(employee, VerificationMethod.FACE, FACE_SUCCESS, confidence=score)

    def migrate_references(self, employee: Employee, face_service: FaceService) -> list[np.ndarray]:
        """Sin embeddings del modelo actual: se generan desde la foto de referencia aprobada."""
        enrollment = FaceEnrollmentRepository(self.db, employee.company_id).latest_for_employee(employee.id)
        if enrollment is None or enrollment.status != EnrollmentStatus.APPROVED or enrollment.photo_encrypted is None:
            return []
        return face_service.migrate_from_photo(employee.id, enrollment.id, decrypt_bytes(enrollment.photo_encrypted))

    def verify_qr(self, user: User, qr_content: str) -> VerificationResult:
        employee = self._employee_of(user)
        PolicyService(self.db, employee.company_id).ensure_qr_enabled()
        lookup = QrService(self.db).lookup(qr_content)

        reason = lookup.reason
        # El QR debe pertenecer a la cuenta autenticada.
        if reason is None and lookup.qr is not None and lookup.qr.employee_id != employee.id:
            reason = "OTHER_EMPLOYEE"

        if reason is not None:
            self._record(employee, user, VerificationMethod.QR, False, None, reason)
            return failed(VerificationMethod.QR, qr_message(reason))

        self._record(employee, user, VerificationMethod.QR, True, None, None)
        return succeeded(employee, VerificationMethod.QR, QR_SUCCESS, confidence=None)

    # ---------- Internos ----------

    @staticmethod
    def _employee_of(user: User) -> Employee:
        employee = user.employee
        if employee is None:
            raise PermissionDeniedError("Solo los empleados pueden verificarse")
        if not employee.active or not user.active:
            raise PermissionDeniedError("El empleado está inactivo", code="USER_INACTIVE")
        if employee.face_status != FaceStatus.APPROVED:
            raise PermissionDeniedError(
                {
                    FaceStatus.PENDING_REVIEW: "Tu registro facial está en validación por tu empresa",
                    FaceStatus.REJECTED: "Tu registro facial fue rechazado. Regístrate de nuevo",
                }.get(employee.face_status, "Primero debes registrar tu rostro"),
                code="FACE_NOT_APPROVED",
            )
        return employee

    def _record(
        self,
        employee: Employee,
        user: User,
        method: VerificationMethod,
        success: bool,
        score: float | None,
        reason: str | None,
    ) -> None:
        self.log.record(
            company_id=employee.company_id,
            employee_id=employee.id,
            actor_id=user.id,
            method=method,
            success=success,
            score=score,
            reason=reason,
        )
