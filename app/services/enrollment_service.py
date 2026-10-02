"""Registro facial hecho por el propio empleado y validado por COMPANY.

Flujo:
    EMPLOYEE (primer inicio de sesión, estado NOT_ENROLLED o REJECTED)
      → captura frontal x3 + prueba de vida → submit()
      → embeddings guardados INACTIVOS + foto de referencia cifrada → PENDING_REVIEW
    COMPANY revisa la foto y los datos del empleado
      → approve(): activa los embeddings → APPROVED (ya puede verificarse)
      → reject(motivo): borra embeddings y foto → REJECTED (debe registrarse de nuevo)
"""

import base64
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_bytes, encrypt_bytes
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, UnprocessableError
from app.facial_recognition import FacePipeline
from app.models import EnrollmentStatus, FaceEnrollment, FaceEnrollmentFlag, FaceStatus, User
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.schemas.enrollment import (
    EnrollmentSubmitResponse,
    FaceEnrollmentDetail,
    FaceEnrollmentList,
    FaceEnrollmentRead,
)
from app.services.catalog_service import get_catalogs
from app.services.face_service import SPOOF_FLAG, FaceService, accessories_rejection
from app.services.identity_core import liveness_failure
from app.services.liveness_service import challenge_store
from app.services.policy_service import PolicyService

ENROLL_FRAMES = (1, 5)


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class EnrollmentService:
    """Registros faciales de UNA empresa: el empleado envía el suyo, su COMPANY los valida."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = FaceEnrollmentRepository(db, company_id)
        self.embeddings = FaceEmbeddingRepository(db)

    # ------------------------------------------------------------------ EMPLOYEE

    def submit(
        self,
        user: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        *,
        challenge_id: str | None,
        challenge_image: bytes | None,
        accessory_review: bool = False,
    ) -> EnrollmentSubmitResponse:
        employee = user.employee
        if employee is None or not employee.active or employee.company_id != self.company_id:
            raise PermissionDeniedError("Solo empleados activos pueden registrar su rostro")
        if employee.face_status == FaceStatus.PENDING_REVIEW:
            raise ConflictError("Tu registro facial ya fue enviado y está en validación", code="ENROLLMENT_PENDING")
        if employee.face_status == FaceStatus.APPROVED:
            raise ConflictError("Tu registro facial ya fue aprobado", code="ENROLLMENT_APPROVED")
        if not ENROLL_FRAMES[0] <= len(frontal_images) <= ENROLL_FRAMES[1]:
            raise UnprocessableError("Envía entre 1 y 5 capturas frontales", code="INVALID_FRAME_COUNT")

        policy = PolicyService(self.db, self.company_id).current()
        challenge = challenge_store.require(
            self.db, user.id, challenge_id, challenge_image, required=policy.liveness_required
        )

        face_service = FaceService(self.db, pipeline)
        # Calidad, pose y consistencia entre muestras (422 si algo falla). Accesorios: 422 salvo que
        # el empleado pida revisión. Suplantación: nunca bloquea el registro, se marca al revisor.
        analyses, flags = face_service.analyze_enrollment(
            frontal_images, policy=policy.face_policy(employee), allow_review=True
        )
        accessories = [f for f in flags if f != SPOOF_FLAG]
        if accessories and not accessory_review:
            raise accessories_rejection(accessories)

        failure = liveness_failure(pipeline, challenge, challenge_image, analyses)
        if failure is not None:
            # Mismo código que la identificación; el texto es el del registro (catalog.face_errors).
            error = failure.capture_error
            code, details = (error.code, error.details) if error else (failure.reason, None)
            raise UnprocessableError(get_catalogs().face_error_message(code, details), code=failure.reason)

        # Reemplaza cualquier registro previo (rechazado) del empleado.
        face_service.delete_all(employee.id)
        enrollment = self.repo.add(
            FaceEnrollment(
                employee_id=employee.id,
                status=EnrollmentStatus.PENDING,
                photo_encrypted=encrypt_bytes(frontal_images[0]),
                photo_content_type=_image_type(frontal_images[0]),
                quality_score=min(a.quality_score for a in analyses),
                samples=len(analyses),
                liveness_passed=challenge is not None,
                flags=[FaceEnrollmentFlag(flag_code=flag) for flag in flags],
            )
        )
        # Inactivos hasta que COMPANY valide la identidad.
        face_service.store(employee.id, analyses, replace=False, active=False, enrollment_id=enrollment.id)
        employee.face_status = FaceStatus.PENDING_REVIEW
        employee.face_rejection_reason = None
        self.db.commit()
        return EnrollmentSubmitResponse(enrollment_id=enrollment.id, face_status=employee.face_status)

    # ------------------------------------------------------------------ COMPANY

    def list_enrollments(self, *, status: EnrollmentStatus | None, page: int, size: int) -> FaceEnrollmentList:
        items, total = self.repo.search(status=status, offset=(page - 1) * size, limit=size)
        return FaceEnrollmentList(items=[self._to_read(e) for e in items], total=total, page=page, size=size)

    def get(self, enrollment_id: int) -> FaceEnrollment:
        enrollment = self.repo.get(enrollment_id)
        if enrollment is None:
            raise NotFoundError("Registro facial no encontrado", code="ENROLLMENT_NOT_FOUND")
        return enrollment

    def detail(self, enrollment_id: int) -> FaceEnrollmentDetail:
        enrollment = self.get(enrollment_id)
        photo = None
        if enrollment.photo_encrypted:
            data = base64.b64encode(decrypt_bytes(enrollment.photo_encrypted)).decode()
            photo = f"data:{enrollment.photo_content_type or 'image/jpeg'};base64,{data}"
        return FaceEnrollmentDetail(**self._to_read(enrollment).model_dump(), photo=photo)

    def latest_for_employee(self, employee_id: int) -> FaceEnrollment | None:
        return self.repo.latest_for_employee(employee_id)

    def approve(self, enrollment_id: int, reviewer: User) -> FaceEnrollmentDetail:
        enrollment = self._pending(enrollment_id)
        enrollment.status = EnrollmentStatus.APPROVED
        enrollment.reviewed_at = datetime.now(UTC)
        enrollment.reviewed_by_id = reviewer.id
        self.embeddings.activate_enrollment(enrollment.id)
        enrollment.employee.face_status = FaceStatus.APPROVED
        enrollment.employee.face_rejection_reason = None
        self.db.commit()
        return self.detail(enrollment.id)

    def reject(self, enrollment_id: int, reviewer: User, reason: str) -> FaceEnrollmentDetail:
        enrollment = self._pending(enrollment_id)
        enrollment.status = EnrollmentStatus.REJECTED
        enrollment.reviewed_at = datetime.now(UTC)
        enrollment.reviewed_by_id = reviewer.id
        enrollment.rejection_reason = reason
        # Minimización de datos: no se conservan biométricos ni fotografía de un registro rechazado.
        enrollment.photo_encrypted = None
        self.embeddings.delete_enrollment(enrollment.id)
        enrollment.employee.face_status = FaceStatus.REJECTED
        enrollment.employee.face_rejection_reason = reason
        self.db.commit()
        return self.detail(enrollment.id)

    # ------------------------------------------------------------------ helpers

    def _pending(self, enrollment_id: int) -> FaceEnrollment:
        enrollment = self.get(enrollment_id)
        if enrollment.status != EnrollmentStatus.PENDING:
            raise ConflictError("Este registro ya fue revisado", code="ENROLLMENT_ALREADY_REVIEWED")
        return enrollment

    def _to_read(self, e: FaceEnrollment) -> FaceEnrollmentRead:
        reviewer = self.db.get(User, e.reviewed_by_id) if e.reviewed_by_id else None
        emp = e.employee
        return FaceEnrollmentRead(
            id=e.id,
            status=e.status,
            employee_id=emp.id,
            employee_number=emp.employee_number,
            full_name=emp.full_name,
            email=emp.user.email,
            birth_date=emp.birth_date,
            employee_active=emp.active,
            samples=e.samples,
            quality_score=e.quality_score,
            liveness_passed=e.liveness_passed,
            flagged_accessories=e.flag_codes,
            submitted_at=e.submitted_at,
            reviewed_at=e.reviewed_at,
            reviewed_by=reviewer.email if reviewer else None,
            rejection_reason=e.rejection_reason,
        )
