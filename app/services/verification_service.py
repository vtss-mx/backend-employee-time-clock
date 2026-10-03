"""Verificación de identidad 1:1 (rostro o QR).

- El EMPLOYEE autenticado se verifica a sí mismo: la evidencia (rostro o QR) debe corresponder a
  la cuenta que inició sesión.
- La COMPANY verifica en persona a uno de sus empleados: su rostro contra el registro aprobado.

Cada intento queda en `verification_logs` (con quién lo hizo). Las reglas comunes viven en
identity_core.
"""

import logging
from collections.abc import Sequence

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_bytes
from app.core.exceptions import ConflictError, PermissionDeniedError
from app.facial_recognition import FacePipeline
from app.models import Employee, EnrollmentStatus, FaceStatus, User, VerificationMethod
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.schemas.verification import FaceChallengeResponse, VerificationResult
from app.services.attempt_guard import ensure_unlocked
from app.services.capture_guard import ensure_real_camera, inspect_take
from app.services.catalog_service import get_catalogs
from app.services.face_service import FaceService, SuspiciousCapture
from app.services.identity_core import (
    FACE_SUCCESS,
    QR_SUCCESS,
    IdentityLog,
    check_liveness,
    ensure_frame_count,
    failed,
    issue_challenge,
    match_references,
    mean_confidence,
    reason_message,
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
        challenge_images: Sequence[bytes] = (),
        camera_label: str | None = None,
    ) -> VerificationResult:
        """Verificación 1:1 del empleado autenticado.

        1. Reto de prueba de vida válido, de uso único y del mismo usuario.
        2. Cada frame frontal: un rostro, calidad, pose frontal y sin accesorios (422 si no).
        3. Frame del reto: la cabeza girada en la dirección solicitada y misma persona.
        4. Cada frame frontal debe alcanzar la confianza de la empresa contra sus muestras.
        """
        return self.verify_employee_face(
            self._employee_of(user),
            user,
            frontal_images,
            pipeline,
            challenge_id=challenge_id,
            challenge_images=challenge_images,
            camera_label=camera_label,
        )

    def verify_in_person(
        self,
        employee: Employee,
        operator: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        *,
        challenge_id: str | None = None,
        challenge_images: Sequence[bytes] = (),
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
            challenge_id=challenge_id,
            challenge_images=challenge_images,
            camera_label=camera_label,
        )

    def verify_employee_face(
        self,
        employee: Employee,
        actor: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        *,
        challenge_id: str | None,
        challenge_images: Sequence[bytes],
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
        references = face_service.load_references(employee.id) or self.migrate_references(employee, face_service)
        if not references:
            raise ConflictError(get_catalogs().face_error_message("FACE_NOT_REGISTERED"), code="FACE_NOT_REGISTERED")

        try:
            ensure_real_camera(camera_label, policy)
            challenge = challenge_store.require(
                self.db, actor.id, challenge_id, challenge_images, required=policy.liveness_required
            )
            # Errores de calidad/accesorios -> 422 (no cuentan como intento fallido).
            face_policy = policy.face_policy(employee)
            frontal, _ = face_service.analyze_frames(frontal_images, policy=face_policy)
            liveness = check_liveness(pipeline, challenge, challenge_images, frontal, policy, face_policy)
            if liveness.spoofed:
                raise SuspiciousCapture("SPOOF_DETECTED")
            inspect_take(self.db, employee.company_id, frontal, liveness.turns, policy)
        except SuspiciousCapture as exc:
            logger.warning("Intento sospechoso (empleado %s, operador %s): %s", employee.id, actor.id, exc.code)
            self._record(employee, actor, VerificationMethod.FACE, False, None, exc.code)
            raise

        failure = liveness.failure
        if failure is not None:
            logger.info("Prueba de vida no superada (empleado %s): %s", employee.id, failure.reason)
            self._record(employee, actor, VerificationMethod.FACE, False, failure.score, failure.reason)
            return failed(VerificationMethod.FACE, failure.message)

        required = required_similarity(policy)
        similarities = match_references(frontal, references, required)
        matched = min(similarities) >= required
        score = mean_confidence(similarities)

        self._record(employee, actor, VerificationMethod.FACE, matched, score, None if matched else "NO_MATCH")
        if not matched:
            return failed(VerificationMethod.FACE, reason_message("NO_MATCH"))
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
            return failed(VerificationMethod.QR, reason_message(reason))

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
