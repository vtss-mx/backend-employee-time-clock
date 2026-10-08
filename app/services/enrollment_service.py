"""Registro facial hecho por el propio empleado y validado por COMPANY.

Flujo (el orden lo fijó el dueño del producto el 2026-10-06 y no se altera; desde el 2026-10-07 son TRES pasos
independientes y retomables, `services/enrollment_steps.py`):
    EMPLOYEE (primer inicio de sesión, estado NOT_ENROLLED o REJECTED)
      → paso 1: foto inicial válida guardada como borrador (`POST /enrollment/photo`, `FaceEnrollmentDraft`)
      → paso 2: 32 fotos VÁLIDAS + prueba de vida → submit(): exige el borrador vigente y que las capturas sean la
        MISMA persona que la foto inicial; el servidor elige las referencias (`enrollment_selection`)
      → embeddings guardados INACTIVOS + UNA foto de referencia cifrada; el borrador se consume
      → paso 3, con `voice_verification`: tres preguntas en video (`POST /enrollment/voice/start` emite la sesión las
        veces que haga falta, conservando las respuestas aceptadas); el registro no termina ni el empleado cambia de
        estado hasta responderlas → PENDING_REVIEW
    COMPANY revisa la foto, el video y los datos del empleado
      → approve(): activa los embeddings → APPROVED (ya puede verificarse)
      → reject(motivo): borra embeddings, foto y videos → REJECTED (debe registrarse de nuevo)
El registro en persona (la empresa con el empleado presente) sigue siendo un solo flujo: no lleva la foto inicial
guardada ni la verificación por voz; la empresa ya ve a la persona y aprueba al momento.
"""

import base64
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.core.object_storage import StorageError
from app.facial_recognition import FaceAnalysis, FacePipeline
from app.facial_recognition.calibration import similarity_for_confidence
from app.i18n import t
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
from app.schemas.avatar import employee_avatar
from app.schemas.common import PageParams
from app.schemas.enrollment import (
    EnrollmentSubmitResponse,
    FaceEnrollmentDetail,
    FaceEnrollmentList,
    FaceEnrollmentRead,
    SimilarEmployee,
)
from app.services import image_storage
from app.services.attempt_guard import ensure_unlocked
from app.services.catalog_service import face_error_text
from app.services.enrollment_selection import EnrollmentSelection, select_references
from app.services.enrollment_steps import Anchor, EnrollmentStepsService, ensure_enrollable
from app.services.face_gallery import duplicate_of, face_galleries, similar_people
from app.services.face_service import (
    SECURITY_REASONS,
    SPOOF_FLAG,
    FaceService,
    SuspiciousCapture,
    accessories_rejection,
    face_rejection,
)
from app.services.identity_core import IdentityLog, LivenessCheck, confirm_live, required_match, take_challenge
from app.services.image_storage import FACE_ENROLLMENT_PHOTOS, ImageUnreadable, image_type
from app.services.liveness_service import Challenge, LivenessResponse, is_enrollment_challenge
from app.services.policy_service import PolicyService, PolicySnapshot
from app.services.voice_verification import VoiceVerificationService

logger = logging.getLogger(__name__)

#: Marca de revisión: el rostro ya está aprobado para otro empleado de la empresa.
DUPLICATE_FLAG = "DUPLICATE_FACE"
#: Marca de revisión con el nivel de SOSPECHA de la empresa (más sensible que el de aceptación): se parece a otros
#: empleados aprobados (sus ids y similitud van en la marca). Nunca bloquea, tampoco en persona.
POSSIBLE_DUPLICATE_FLAG = "POSSIBLE_DUPLICATE"
#: Marcas que solo informan al revisor (no son accesorios que bloqueen el envío).
REVIEW_ONLY_FLAGS = (SPOOF_FLAG, DUPLICATE_FLAG, POSSIBLE_DUPLICATE_FLAG)


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
        """El empleado registra su rostro (paso 2 de 3): exige su foto inicial vigente (paso 1) y que las capturas sean
        de la misma persona; queda en validación hasta que su empresa lo revise (tras el video, si la política lo
        pide)."""
        employee = ensure_enrollable(user.employee, self.company_id)
        # El ancla se lee (y descifra) antes del reto: `take_challenge` cierra esa transacción antes de analizar.
        anchor = EnrollmentStepsService(self.db, self.company_id).anchor_for(employee, pipeline, datetime.now(UTC))
        enrollment = self._capture(
            employee,
            user,
            frontal_images,
            pipeline,
            challenge=liveness,
            accessory_review=accessory_review,
            camera_label=camera_label,
            anchor=anchor,
        )
        # Con la verificación por voz (decisión del dueño, 2026-10-06), las fotos no terminan el registro: siguen las
        # preguntas en video. La sesión viaja aquí por compatibilidad con la app de un solo flujo; la app de tres
        # pasos la pide con `POST /enrollment/voice/start` al abrir el paso 3 (decisión del dueño, 2026-10-07).
        voice = (
            VoiceVerificationService(self.db, self.company_id).start(enrollment, employee)
            if enrollment.voice_required
            else None
        )
        self.db.commit()
        message = t("ENROLLMENT_PHOTOS_ACCEPTED") if voice is not None else t("ENROLLMENT_SENT")
        return EnrollmentSubmitResponse(
            enrollment_id=enrollment.id, face_status=employee.face_status, message=message, voice=voice
        )

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
            raise ConflictError(code="EMPLOYEE_INACTIVE", key="EMPLOYEE_INACTIVE_ENROLL")
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
            message=t("FACE_ENROLLED_IN_PERSON"),
        )

    def list_enrollments(self, *, status: EnrollmentStatus | None, page: PageParams) -> FaceEnrollmentList:
        items, total = self.repo.search(status=status, offset=page.offset, limit=page.size)
        emails = UserRepository(self.db).emails_by_ids(i for e in items for i in (e.reviewed_by_id, e.captured_by_id))
        return FaceEnrollmentList.of([self._to_read(e, emails) for e in items], total, page)

    def get(self, enrollment_id: int) -> FaceEnrollment:
        enrollment = self.repo.get(enrollment_id)
        if enrollment is None:
            raise NotFoundError(code="ENROLLMENT_NOT_FOUND")
        return enrollment

    def detail(self, enrollment_id: int) -> FaceEnrollmentDetail:
        enrollment = self.get(enrollment_id)
        emails = UserRepository(self.db).emails_by_ids((enrollment.reviewed_by_id, enrollment.captured_by_id))
        return FaceEnrollmentDetail(
            **self._to_read(enrollment, emails).model_dump(),
            photo=self._photo(enrollment),
            similar=self._similar(enrollment),
            voice=VoiceVerificationService(self.db, self.company_id).voice_of(enrollment),
        )

    def _similar(self, enrollment: FaceEnrollment) -> list[SimilarEmployee]:
        """Los empleados más parecidos de la marca POSSIBLE_DUPLICATE (los que siguen vigentes: uno en «Eliminados» ya
        no tiene datos faciales con qué compararse), en una consulta."""
        flag = next((f for f in enrollment.flags if f.flag_code == POSSIBLE_DUPLICATE_FLAG and f.details), None)
        if flag is None or flag.details is None:
            return []
        listed = [(int(item["employee_id"]), float(item["similarity"])) for item in flag.details.get("similar", [])]
        people = EmployeeRepository(self.db, self.company_id).by_ids({employee_id for employee_id, _ in listed})
        return [
            SimilarEmployee(
                employee_id=employee_id,
                full_name=people[employee_id].full_name,
                employee_number=people[employee_id].employee_number,
                similarity=similarity,
                avatar=employee_avatar(people[employee_id]),
            )
            for employee_id, similarity in listed
            if employee_id in people and not people[employee_id].deleted
        ]

    @staticmethod
    def _photo(enrollment: FaceEnrollment) -> str | None:
        """La foto de referencia como data URL, leída del bucket. Ilegible o con el bucket caído: el revisor
        ve el registro sin la foto y la falla queda registrada."""
        try:
            raw = image_storage.read(FACE_ENROLLMENT_PHOTOS, enrollment)
        except ImageUnreadable:
            logger.error("La foto del registro facial %s es ilegible", enrollment.id)
            return None
        except StorageError:
            logger.exception("No se pudo leer del bucket la foto del registro facial %s", enrollment.id)
            return None
        if not raw:
            return None
        return f"data:{enrollment.photo_content_type or 'image/jpeg'};base64,{base64.b64encode(raw).decode()}"

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
        # Minimización de datos: no se conservan biométricos, fotografía ni videos de un registro rechazado (sus
        # objetos pasan a la cola de borrado del bucket en esta misma transacción).
        image_storage.forget(self.db, FACE_ENROLLMENT_PHOTOS, enrollment)
        self.repo.release_voice_clips(enrollment.id)
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
        anchor: Anchor | None = None,
    ) -> FaceEnrollment:
        """Valida las capturas y deja el registro en validación (muestras inactivas).

        El reto de prueba de vida es de quien opera la cámara (`actor`): el propio empleado o, en el
        registro asistido, el administrador de la empresa. Los intentos sospechosos se registran en la
        bitácora del empleado y bloquean temporalmente tras varios seguidos. `anchor` (el registro propio): la foto
        inicial, con la que deben coincidir las referencias y que pasa a ser la foto de referencia del registro
        (decisión del dueño, 2026-10-07).

        Llegan hasta FACE_ENROLL_MAX_PHOTOS fotos (36; decisión del dueño, 2026-10-06) y el servidor elige las
        referencias (`enrollment_selection`): solo esas se guardan como plantilla y solo una foto queda para el revisor
        (la foto inicial en el registro propio; la mejor de las elegidas en persona). Se aceptan desde una foto (una app
        anterior manda 5 durante un despliegue gradual); la selección exige FACE_ENROLL_MIN_USABLE útiles.
        """
        if not 1 <= len(frontal_images) <= settings.FACE_ENROLL_MAX_PHOTOS:
            raise UnprocessableError(
                code="INVALID_FRAME_COUNT", params={"min": 1, "max": settings.FACE_ENROLL_MAX_PHOTOS}
            )
        policy = PolicyService(self.db, self.company_id).current()
        ensure_unlocked(self.db, policy, employee_id=employee.id, reasons=SECURITY_REASONS)
        try:
            selection, flags, issued, liveness = self._guarded_analysis(
                employee,
                actor,
                frontal_images,
                pipeline,
                challenge,
                policy,
                in_person=in_person,
                camera_label=camera_label,
                anchor=anchor,
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
            raise UnprocessableError(face_error_text(code, details), code=failure.reason)

        # La misma persona registrada como otro empleado: en persona se bloquea; si no, se marca al revisor.
        references = list(selection.references)
        duplicate = self._duplicate_of(employee, references, pipeline, policy)
        if duplicate is not None and in_person:
            message = face_error_text("FACE_ALREADY_REGISTERED")
            number = duplicate.employee_number  # opcional (migración 0076): sin número, solo el nombre
            raise ConflictError(
                code="FACE_ALREADY_REGISTERED",
                key="FACE_ALREADY_REGISTERED_AS" if number else "FACE_ALREADY_REGISTERED_AS_NAME",
                params={"message": message, "name": duplicate.full_name, "number": number},
                details={"employee_id": duplicate.id},
            )
        if duplicate is not None:
            flags = (*flags, DUPLICATE_FLAG)
        flag_details: dict[str, dict[str, Any]] = {}
        similar = self._similar_to(employee, references, pipeline, policy) if duplicate is None else []
        if similar:
            flags = (*flags, POSSIBLE_DUPLICATE_FLAG)
            flag_details[POSSIBLE_DUPLICATE_FLAG] = {
                "similar": [{"employee_id": other, "similarity": similarity} for other, similarity in similar]
            }

        # Reemplaza cualquier registro previo del empleado (también uno que esperaba sus respuestas en video: su foto y
        # sus clips salen del bucket).
        face_service = FaceService(self.db, pipeline)
        face_service.delete_all(employee.id)
        self.repo.delete_unfinished(employee.id)
        photo = frontal_images[selection.photo]
        voice_required = policy.voice_verification and not in_person
        # Registro propio: la foto inicial (ya cifrada en el bucket desde el paso 1) es la foto de referencia; su
        # referencia va en la misma inserción del registro. En persona, la mejor de las elegidas se sube abajo.
        reference = (
            EnrollmentStepsService(self.db, self.company_id).hand_over(anchor)
            if anchor is not None
            else {"photo_content_type": image_type(photo)}
        )
        enrollment = self.repo.add(
            FaceEnrollment(
                employee_id=employee.id,
                status=EnrollmentStatus.PENDING,
                **reference,
                quality_score=min(a.quality_score for a in references),
                samples=len(references),
                liveness_passed=issued is not None,
                captured_by_id=actor.id if in_person else None,
                voice_required=voice_required,
                flags=[FaceEnrollmentFlag(flag_code=flag, details=flag_details.get(flag)) for flag in flags],
            )
        )
        if anchor is None:
            # En persona: la de mejor calidad de las elegidas (ninguna otra se conserva) va CIFRADA al bucket (nunca a
            # la BD), ya con el id del registro y antes de confirmar. Sin bucket, 503 STORAGE_UNAVAILABLE y no queda
            # nada a medias.
            image_storage.store(self.db, FACE_ENROLLMENT_PHOTOS, enrollment, photo)
        # Inactivos hasta que COMPANY valide la identidad.
        face_service.store(employee, references, active=False, enrollment_id=enrollment.id)
        if not voice_required:  # con la voz pendiente, el estado cambia al pasar la última respuesta
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
        anchor: Anchor | None = None,
    ) -> tuple[EnrollmentSelection, tuple[str, ...], Challenge | None, LivenessCheck]:
        """Cámara real, reto, las referencias elegidas entre las fotos (calidad, una sola persona y, con `anchor`, la
        persona de la foto inicial), prueba de vida y toma en vivo.

        Suplantación: en el autoregistro se marca al revisor; en el registro asistido bloquea (se
        aprueba al momento, no habrá revisión que la descarte). La toma única recuerda las huellas de TODAS las fotos
        útiles (`others`), no solo las de las referencias: ninguna sirve después en otro registro.
        """
        issued = take_challenge(self.db, actor.id, challenge, camera_label, policy, frontal_images)
        # Decisión del dueño (2026-10-07): la prueba de vida del registro es completa (los cuatro movimientos). Un reto
        # de verificación (1 a 3 pasos) no sirve para registrarse: falta algún movimiento.
        if issued is not None and not is_enrollment_challenge(issued):
            raise face_rejection("LIVENESS_REQUIRED")
        face_policy = policy.face_policy(employee)
        selection = select_references(pipeline, frontal_images, face_policy)
        if anchor is not None:  # paso 2 del registro propio: las capturas son la persona de la foto inicial (CPU)
            EnrollmentStepsService.ensure_same_person(anchor, list(selection.references))
        flags = selection.flags
        liveness = confirm_live(
            self.db,
            self.company_id,
            pipeline,
            (issued, challenge),
            selection.references,
            policy,
            face_policy,
            block_spoof=in_person,
            flagged_spoof=SPOOF_FLAG in flags,
            others=selection.usable,
        )
        if liveness.spoofed and SPOOF_FLAG not in flags:
            flags = (*flags, SPOOF_FLAG)
        return selection, flags, issued, liveness

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

    def _similar_to(
        self, employee: Employee, analyses: list[FaceAnalysis], pipeline: FacePipeline, policy: PolicySnapshot
    ) -> list[tuple[int, float]]:
        """Los empleados aprobados a los que se parece el rostro con el nivel de SOSPECHA de la empresa
        (`duplicate_confidence`, más sensible que el de aceptación: un *morph* o un parecido queda bajo el de
        aceptación pero no bajo este). Solo marca para la revisión (fase 0 del antifraude)."""
        if not policy.detect_duplicate_faces:
            return []
        gallery = face_galleries.get(self.db, self.company_id, pipeline.model_name)
        required = similarity_for_confidence(policy.duplicate_confidence, settings.FACE_RECOGNITION_MODEL)
        return similar_people(
            gallery,
            [a.embedding for a in analyses],
            exclude=employee.id,
            required=required,
            top=settings.FACE_DUPLICATE_SIMILAR_SHOWN,
        )

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
            raise NotFoundError(code="ENROLLMENT_NOT_FOUND")
        if enrollment.status != EnrollmentStatus.PENDING:
            raise ConflictError(code="ENROLLMENT_ALREADY_REVIEWED")
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
            # La cuenta viene con el empleado (JOIN de su carga): sin consultas de más.
            avatar=employee_avatar(emp),
        )
