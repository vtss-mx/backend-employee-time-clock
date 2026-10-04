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
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.crypto import encrypt_bytes, try_decrypt
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, UnprocessableError
from app.facial_recognition import FaceAnalysis, FacePipeline
from app.models import (
    Employee,
    EnrollmentStatus,
    FaceEnrollment,
    FaceEnrollmentFlag,
    FaceStatus,
    User,
    VerificationMethod,
)
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.repositories.user_repository import UserRepository
from app.schemas.common import PageParams
from app.schemas.enrollment import (
    EnrollmentSubmitResponse,
    FaceEnrollmentDetail,
    FaceEnrollmentList,
    FaceEnrollmentRead,
)
from app.services.attempt_guard import ensure_unlocked
from app.services.catalog_service import get_catalogs
from app.services.face_gallery import duplicate_of, face_galleries
from app.services.face_service import (
    SECURITY_REASONS,
    SPOOF_FLAG,
    FaceService,
    SuspiciousCapture,
    accessories_rejection,
)
from app.services.identity_core import IdentityLog, LivenessCheck, confirm_live, required_match, take_challenge
from app.services.liveness_service import Challenge, LivenessResponse
from app.services.policy_service import PolicyService, PolicySnapshot

logger = logging.getLogger(__name__)

#: Marca de revisión: el rostro ya está aprobado para otro empleado de la empresa.
DUPLICATE_FLAG = "DUPLICATE_FACE"
#: Marcas que solo informan al revisor (no son accesorios que bloqueen el envío).
REVIEW_ONLY_FLAGS = (SPOOF_FLAG, DUPLICATE_FLAG)

ENROLL_FRAMES = (1, 5)


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class EnrollmentService:
    """Registros faciales de UNA empresa: el empleado envía el suyo y su COMPANY lo valida, o la
    COMPANY lo captura en persona (registro asistido, aprobado al momento)."""

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
        liveness: LivenessResponse,
        accessory_review: bool = False,
        camera_label: str | None = None,
    ) -> EnrollmentSubmitResponse:
        """El empleado registra su rostro: queda en validación hasta que su empresa lo revise."""
        employee = user.employee
        if employee is None or not employee.active or employee.company_id != self.company_id:
            raise PermissionDeniedError("Solo empleados activos pueden registrar su rostro")
        if employee.face_status == FaceStatus.PENDING_REVIEW:
            raise ConflictError("Tu registro facial ya fue enviado y está en validación", code="ENROLLMENT_PENDING")
        if employee.face_status == FaceStatus.APPROVED:
            raise ConflictError("Tu registro facial ya fue aprobado", code="ENROLLMENT_APPROVED")
        enrollment = self._capture(
            employee,
            user,
            frontal_images,
            pipeline,
            challenge=liveness,
            accessory_review=accessory_review,
            camera_label=camera_label,
        )
        self.db.commit()
        return EnrollmentSubmitResponse(enrollment_id=enrollment.id, face_status=employee.face_status)

    # ------------------------------------------------------------------ COMPANY

    def enroll_in_person(
        self,
        employee: Employee,
        operator: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        *,
        liveness: LivenessResponse,
        camera_label: str | None = None,
    ) -> EnrollmentSubmitResponse:
        """Registro asistido: la empresa captura el rostro del empleado con el empleado presente.

        Mismas validaciones que el autoregistro (calidad, pose, accesorios, consistencia y prueba de
        vida), pero queda APROBADO al momento: la empresa vio a la persona. Por eso aquí la sospecha
        de foto o pantalla bloquea (no habrá una revisión posterior que la descarte). Reemplaza
        cualquier registro anterior del empleado.
        """
        if not employee.active:
            raise ConflictError(
                "El empleado está inactivo: actívalo antes de registrar su rostro", code="EMPLOYEE_INACTIVE"
            )
        enrollment = self._capture(
            employee,
            operator,
            frontal_images,
            pipeline,
            challenge=liveness,
            in_person=True,
            camera_label=camera_label,
        )
        self._approve(enrollment, operator)
        self.db.commit()
        return EnrollmentSubmitResponse(
            enrollment_id=enrollment.id,
            face_status=employee.face_status,
            message="Rostro registrado y aprobado: el empleado ya puede identificarse",
        )

    def list_enrollments(self, *, status: EnrollmentStatus | None, page: PageParams) -> FaceEnrollmentList:
        items, total = self.repo.search(status=status, offset=page.offset, limit=page.size)
        emails = UserRepository(self.db).emails_by_ids(i for e in items for i in (e.reviewed_by_id, e.captured_by_id))
        return FaceEnrollmentList.of([self._to_read(e, emails) for e in items], total, page)

    def get(self, enrollment_id: int) -> FaceEnrollment:
        enrollment = self.repo.get(enrollment_id)
        if enrollment is None:
            raise NotFoundError("Registro facial no encontrado", code="ENROLLMENT_NOT_FOUND")
        return enrollment

    def detail(self, enrollment_id: int) -> FaceEnrollmentDetail:
        enrollment = self.get(enrollment_id)
        photo = None
        if enrollment.photo_encrypted:
            raw = try_decrypt(enrollment.photo_encrypted)
            if raw is None:  # dañada o de otra llave: el revisor ve el registro sin la foto
                logger.error("La foto del registro facial %s es ilegible", enrollment.id)
            else:
                data = base64.b64encode(raw).decode()
                photo = f"data:{enrollment.photo_content_type or 'image/jpeg'};base64,{data}"
        emails = UserRepository(self.db).emails_by_ids((enrollment.reviewed_by_id, enrollment.captured_by_id))
        return FaceEnrollmentDetail(**self._to_read(enrollment, emails).model_dump(), photo=photo)

    def approve(self, enrollment_id: int, reviewer: User) -> FaceEnrollmentDetail:
        enrollment = self._pending(enrollment_id)
        self._approve(enrollment, reviewer)
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

    def _capture(
        self,
        employee: Employee,
        actor: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        *,
        challenge: LivenessResponse,
        accessory_review: bool = False,
        in_person: bool = False,
        camera_label: str | None = None,
    ) -> FaceEnrollment:
        """Valida las capturas y deja el registro en validación (muestras inactivas).

        El reto de prueba de vida es de quien opera la cámara (`actor`): el propio empleado o, en el
        registro asistido, el administrador de la empresa. Los intentos sospechosos se registran en la
        bitácora del empleado y bloquean temporalmente tras varios seguidos.
        """
        if not ENROLL_FRAMES[0] <= len(frontal_images) <= ENROLL_FRAMES[1]:
            raise UnprocessableError("Envía entre 1 y 5 capturas frontales", code="INVALID_FRAME_COUNT")
        policy = PolicyService(self.db, self.company_id).current()
        ensure_unlocked(self.db, policy, employee_id=employee.id, reasons=SECURITY_REASONS)
        try:
            analyses, flags, issued, liveness = self._guarded_analysis(
                employee,
                actor,
                frontal_images,
                pipeline,
                challenge,
                policy,
                in_person=in_person,
                camera_label=camera_label,
            )
        except SuspiciousCapture as exc:
            IdentityLog(self.db, ip=None, user_agent=None).record(
                company_id=self.company_id,
                employee_id=employee.id,
                actor_id=actor.id,
                method=VerificationMethod.FACE,
                success=False,
                reason=exc.code,
            )
            raise
        accessories = [f for f in flags if f not in REVIEW_ONLY_FLAGS]
        if accessories and not accessory_review:
            raise accessories_rejection(accessories)

        failure = liveness.failure
        if failure is not None:
            # Mismo código que la identificación; el texto es el del registro (catalog.face_errors).
            error = failure.capture_error
            code, details = (error.code, error.details) if error else (failure.reason, None)
            raise UnprocessableError(get_catalogs().face_error_message(code, details), code=failure.reason)

        # La misma persona registrada como otro empleado: en persona se bloquea; si no, se marca al revisor.
        duplicate = self._duplicate_of(employee, analyses, pipeline, policy)
        if duplicate is not None and in_person:
            raise ConflictError(
                f"{get_catalogs().face_error_message('FACE_ALREADY_REGISTERED')} ({duplicate.full_name}, "
                f"{duplicate.employee_number})",
                code="FACE_ALREADY_REGISTERED",
                details={"employee_id": duplicate.id},
            )
        if duplicate is not None:
            flags = (*flags, DUPLICATE_FLAG)

        # Reemplaza cualquier registro previo del empleado.
        face_service = FaceService(self.db, pipeline)
        face_service.delete_all(employee.id)
        enrollment = self.repo.add(
            FaceEnrollment(
                employee_id=employee.id,
                status=EnrollmentStatus.PENDING,
                photo_encrypted=encrypt_bytes(frontal_images[0]),
                photo_content_type=_image_type(frontal_images[0]),
                quality_score=min(a.quality_score for a in analyses),
                samples=len(analyses),
                liveness_passed=issued is not None,
                captured_by_id=actor.id if in_person else None,
                flags=[FaceEnrollmentFlag(flag_code=flag) for flag in flags],
            )
        )
        # Inactivos hasta que COMPANY valide la identidad.
        face_service.store(employee.id, analyses, active=False, enrollment_id=enrollment.id)
        employee.face_status = FaceStatus.PENDING_REVIEW
        employee.face_rejection_reason = None
        return enrollment

    def _guarded_analysis(
        self,
        employee: Employee,
        actor: User,
        frontal_images: list[bytes],
        pipeline: FacePipeline,
        challenge: LivenessResponse,
        policy: PolicySnapshot,
        *,
        in_person: bool,
        camera_label: str | None,
    ) -> tuple[list[FaceAnalysis], tuple[str, ...], Challenge | None, LivenessCheck]:
        """Cámara real, reto, calidad y consistencia de las muestras, prueba de vida y toma en vivo.

        Suplantación: en el autoregistro se marca al revisor; en el registro asistido bloquea (se
        aprueba al momento, no habrá revisión que la descarte).
        """
        issued = take_challenge(self.db, actor.id, challenge, camera_label, policy)
        face_policy = policy.face_policy(employee)
        analyses, flags = FaceService(self.db, pipeline).analyze_enrollment(
            frontal_images, policy=face_policy, allow_review=True
        )
        liveness = confirm_live(
            self.db,
            self.company_id,
            pipeline,
            (issued, challenge),
            analyses,
            policy,
            face_policy,
            block_spoof=in_person,
            flagged_spoof=SPOOF_FLAG in flags,
        )
        if liveness.spoofed and SPOOF_FLAG not in flags:
            flags = (*flags, SPOOF_FLAG)
        return analyses, flags, issued, liveness

    def _duplicate_of(
        self, employee: Employee, analyses: list[FaceAnalysis], pipeline: FacePipeline, policy: PolicySnapshot
    ) -> Employee | None:
        """Otro empleado de la empresa (ya aprobado) con este mismo rostro (si la empresa lo revisa)."""
        if not policy.detect_duplicate_faces:
            return None
        gallery = face_galleries.get(self.db, self.company_id, pipeline.model_name)
        found = duplicate_of(
            gallery, [a.embedding for a in analyses], exclude=employee.id, required=required_match(policy)
        )
        return EmployeeRepository(self.db, self.company_id).get_by_id(found) if found is not None else None

    def _approve(self, enrollment: FaceEnrollment, reviewer: User) -> None:
        """La empresa acepta la identidad: se activan sus muestras y el empleado ya puede identificarse."""
        enrollment.status = EnrollmentStatus.APPROVED
        enrollment.reviewed_at = datetime.now(UTC)
        enrollment.reviewed_by_id = reviewer.id
        self.embeddings.activate_enrollment(enrollment.id)
        enrollment.employee.face_status = FaceStatus.APPROVED
        enrollment.employee.face_rejection_reason = None

    def _pending(self, enrollment_id: int) -> FaceEnrollment:
        """El registro por revisar, bloqueado hasta el commit (sin carreras entre aprobar y rechazar)."""
        enrollment = self.repo.get(enrollment_id, for_update=True)
        if enrollment is None:
            raise NotFoundError("Registro facial no encontrado", code="ENROLLMENT_NOT_FOUND")
        if enrollment.status != EnrollmentStatus.PENDING:
            raise ConflictError("Este registro ya fue revisado", code="ENROLLMENT_ALREADY_REVIEWED")
        return enrollment

    @staticmethod
    def _to_read(e: FaceEnrollment, emails: dict[int, str]) -> FaceEnrollmentRead:
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
            reviewed_by=emails.get(e.reviewed_by_id) if e.reviewed_by_id else None,
            captured_by=emails.get(e.captured_by_id) if e.captured_by_id else None,
            rejection_reason=e.rejection_reason,
        )
