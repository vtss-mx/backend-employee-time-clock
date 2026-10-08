"""Los tres pasos INDEPENDIENTES del registro facial del propio empleado (decisión del dueño del producto, 2026-10-07).

«Los procesos de orquestación del lado del frontend para la autenticación deben ser independientes: debe haber una
opción para tomar la foto, otra para el enrolamiento y otra para tomar el video y contestar las preguntas». El orden
sigue siendo fijo y lo exige el SERVIDOR; lo que cambia es que cada paso se hace por separado, se puede dejar y retomar
otro día, y nada se pierde:

1. **Foto inicial** (`photograph`, `POST /enrollment/photo`): la misma validación de siempre (nítida, con luz, rostro
   completo y de frente; los accesorios por consenso para las insignias) y, aceptada, queda como BORRADOR del registro
   (`FaceEnrollmentDraft`): la foto cifrada en el bucket y su plantilla facial cifrada, con vencimiento
   `FACE_ENROLLMENT_DRAFT_HOURS`. Un empleado tiene a lo más un borrador: repetir la foto lo reemplaza.
2. **Capturas y prueba de vida** (`POST /enrollment/face`, `EnrollmentService.submit`): exige un borrador vigente
   (`anchor_for`: 409 `ENROLLMENT_PHOTO_REQUIRED`, con la llave `ENROLLMENT_PHOTO_EXPIRED` si venció) y comprueba que
   las referencias elegidas entre las capturas son la MISMA persona que la foto inicial (`ensure_same_person`: la
   mediana de la similitud contra las referencias ≥ `FACE_ENROLL_CONSISTENCY_THRESHOLD`, el umbral de consistencia que
   ya exige el registro; si no, 422 `ENROLLMENT_PHOTO_MISMATCH` y la foto inicial se conserva). Al aceptarlas, la foto
   inicial PASA A SER la foto de referencia del registro (`hand_over`: la que revisa la empresa; su objeto cifrado ya
   está en el bucket y solo cambia de dueño, así el paso más pesado no sube nada; su referencia viaja en la inserción
   del registro) y el borrador sale en una sentencia atómica (`DELETE … RETURNING`: si otra petición lo reemplazó a la
   mitad, 409 y nada se guarda).
3. **Video con preguntas** (`start_voice`, `POST /enrollment/voice/start`): emite la sesión de voz del registro
   pendiente cuantas veces haga falta (409 `VOICE_NOT_PENDING` sin registro pendiente; 422 `VOICE_RETRIES_EXHAUSTED` con
   los intentos agotados: se repiten los pasos 1 y 2), conservando las respuestas aceptadas: la sesión nueva mantiene
   sus preguntas en su posición como ya pasadas y elige al azar solo las que faltan (`VoiceVerificationService.start`).

`progress` (`GET /enrollment/progress`) deriva el estado de los tres pasos sin columnas nuevas: del borrador, del
registro pendiente (y sus intentos) y de sus respuestas aceptadas; lecturas acotadas por sus índices (con el video
pendiente, además las preguntas elegibles de la persona, para decir «2 de 3»).

Transacciones cortas (§4 del AGENTS.md): la foto inicial cierra lo leído (empleado, política) ANTES de analizar
(CPU) y abre una transacción corta para guardar el borrador (la subida al bucket va dentro de ella, como la foto de
referencia del registro: si falla, nada queda a medias). El ancla del paso 2 se lee y descifra antes del reto
(`take_challenge` confirma esa transacción) y se compara después del análisis, sin conexión abierta.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.crypto import encrypt_bytes
from app.core.exceptions import ConflictError, PermissionDeniedError, UnprocessableError
from app.facial_recognition import FaceAnalysis, FacePipeline
from app.facial_recognition.matcher import embedding_to_bytes, similarity_matrix
from app.models import Employee, EnrollmentStatus, FaceEnrollment, FaceEnrollmentDraft, FaceStatus, User
from app.repositories.enrollment_draft_repository import FaceEnrollmentDraftRepository
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.schemas.enrollment import (
    CaptureStep,
    EnrollmentPhotoResponse,
    EnrollmentProgress,
    PhotoStep,
    VoiceChallengeResponse,
    VoiceStep,
)
from app.services import image_storage
from app.services.employee_access import active_employee
from app.services.face_capture_service import FaceCaptureService
from app.services.face_service import readable_embedding
from app.services.image_storage import FACE_ENROLLMENT_DRAFT_PHOTOS, FACE_ENROLLMENT_PHOTOS, image_type
from app.services.policy_service import PolicyService
from app.services.voice_verification import VoiceVerificationService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Anchor:
    """La foto inicial con que se ancla el paso 2: su borrador (que el registro tomará) y su plantilla descifrada."""

    draft_id: int
    vector: np.ndarray


def ensure_enrollable(employee: Employee | None, company_id: int) -> Employee:
    """El empleado que puede registrar su rostro (activo, de esta empresa, sin registro en validación ni aprobado):
    la misma regla para la foto inicial y para las capturas."""
    if employee is None or not employee.active or employee.company_id != company_id:
        raise PermissionDeniedError(key="ENROLLMENT_EMPLOYEE_INACTIVE")
    if employee.face_status == FaceStatus.PENDING_REVIEW:
        raise ConflictError(code="ENROLLMENT_PENDING")
    if employee.face_status == FaceStatus.APPROVED:
        raise ConflictError(code="ENROLLMENT_APPROVED", key="ENROLLMENT_ALREADY_APPROVED")
    return employee


class EnrollmentStepsService:
    """Los pasos del registro facial de UNA empresa (el borrador, el ancla del paso 2, la sesión de voz y el avance)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.drafts = FaceEnrollmentDraftRepository(db, company_id)
        self.enrollments = FaceEnrollmentRepository(db, company_id)

    # ------------------------------------------------------------------ paso 1: la foto inicial

    def photograph(self, user: User, images: list[bytes], pipeline: FacePipeline) -> EnrollmentPhotoResponse:
        """Valida la foto inicial como la validación previa y la guarda como borrador (reemplazando el anterior)."""
        employee = ensure_enrollable(user.employee, self.company_id)
        capture = FaceCaptureService(self.db, user, self.company_id)
        policy = capture.frontal_policy(allow_headwear=False)
        self.db.commit()  # lo leído (empleado, política) se cierra antes del análisis (CPU)
        analyses, detected = capture.analyze(pipeline, images, policy)
        best = max(range(len(analyses)), key=lambda k: (analyses[k].quality_score, analyses[k].detection_score, -k))
        analysis = analyses[best]
        now = datetime.now(UTC)
        draft = FaceEnrollmentDraft(
            template_encrypted=encrypt_bytes(embedding_to_bytes(analysis.embedding)),
            model_name=pipeline.model_name,
            dimension=int(analysis.embedding.shape[0]),
            detection_score=analysis.detection_score,
            quality_score=analysis.quality_score,
            photo_content_type=image_type(images[best]),
            checked_at=now,
            expires_at=now + timedelta(hours=settings.FACE_ENROLLMENT_DRAFT_HOURS),
        )
        self.drafts.replace(employee.id, draft, now)
        # La foto va CIFRADA al bucket (nunca a la BD) con el id del borrador; sin bucket, 503 y nada a medias.
        image_storage.store(self.db, FACE_ENROLLMENT_DRAFT_PHOTOS, draft, images[best])
        self.db.commit()
        return EnrollmentPhotoResponse(
            **FaceCaptureService.report(analysis, detected).model_dump(),
            checked_at=draft.checked_at,
            expires_at=draft.expires_at,
        )

    # ------------------------------------------------------------------ paso 2: el ancla de las capturas

    def anchor_for(self, employee: Employee, pipeline: FacePipeline, now: datetime) -> Anchor:
        """La foto inicial vigente del empleado (su borrador y su plantilla): sin ella (o vencida, ilegible o de otro
        modelo facial), el paso 2 no procede (409) y hay que repetir la foto."""
        draft = self.drafts.of_employee(employee.id)
        if draft is None:
            raise ConflictError(code="ENROLLMENT_PHOTO_REQUIRED")
        if not draft.active_at(now):
            raise ConflictError(code="ENROLLMENT_PHOTO_REQUIRED", key="ENROLLMENT_PHOTO_EXPIRED")
        vector = (
            readable_embedding(draft.id, draft.template_encrypted, draft.dimension)
            if draft.model_name == pipeline.model_name
            else None
        )
        if vector is None:  # ilegible (otra llave de cifrado) o de un motor anterior: no hay con qué comparar
            logger.info("La foto inicial %s del empleado %s no sirve de ancla: se pide de nuevo", draft.id, employee.id)
            raise ConflictError(code="ENROLLMENT_PHOTO_REQUIRED", key="ENROLLMENT_PHOTO_EXPIRED")
        return Anchor(draft_id=draft.id, vector=vector)

    @staticmethod
    def ensure_same_person(anchor: Anchor, references: list[FaceAnalysis]) -> None:
        """Las referencias elegidas entre las capturas son la persona de la foto inicial: la mediana de su similitud
        con ellas alcanza el umbral de consistencia del registro (una referencia rara no decide; una persona distinta
        no pasa). Solo CPU."""
        similarity = similarity_matrix([anchor.vector], [reference.embedding for reference in references])
        if float(np.median(similarity)) < settings.FACE_ENROLL_CONSISTENCY_THRESHOLD:
            raise UnprocessableError(code="ENROLLMENT_PHOTO_MISMATCH")

    def hand_over(self, anchor: Anchor) -> dict[str, Any]:
        """Las capturas se aceptaron: la foto inicial pasa a ser la foto de referencia del registro (la que revisa la
        empresa y de la que se migra el rostro si cambia el motor): su objeto CIFRADO ya está en el bucket y solo
        cambia de dueño (ninguna subida más en el paso más pesado). Devuelve las columnas de la referencia para la
        INSERCIÓN del registro (así no hace falta otra sentencia para anotarlas). El borrador se toma en una sentencia
        atómica: si otra petición lo reemplazó mientras se analizaban las capturas, 409 y nada se guarda (se repiten
        las capturas)."""
        draft = self.drafts.take(anchor.draft_id)
        if draft is None:
            raise ConflictError(code="ENROLLMENT_PHOTO_REQUIRED")
        columns = zip(
            FACE_ENROLLMENT_PHOTOS.reference_columns, FACE_ENROLLMENT_DRAFT_PHOTOS.reference_columns, strict=True
        )
        # Tipo, objeto, tamaño, SHA-256 y cuándo se subió.
        return {"photo_content_type": draft.photo_content_type} | {
            target.key: getattr(draft, source.key) for target, source in columns
        }

    # ------------------------------------------------------------------ paso 3: la sesión de voz

    def start_voice(self, user: User) -> VoiceChallengeResponse:
        """La sesión de preguntas del registro pendiente del empleado, conservando las respuestas aceptadas."""
        employee = active_employee(user)
        enrollment = self._pending(employee)
        if enrollment is None:
            raise ConflictError(code="VOICE_NOT_PENDING")
        voice = VoiceVerificationService(self.db, self.company_id)
        session = voice.start(enrollment, employee, answered=self.enrollments.voice_answers(enrollment.id))
        if enrollment.voice_attempts >= settings.VOICE_MAX_RETRIES_PER_QUESTION * session.total:
            raise UnprocessableError(code="VOICE_RETRIES_EXHAUSTED")
        return session

    # ------------------------------------------------------------------ el índice: dónde va cada paso

    def progress(self, user: User) -> EnrollmentProgress:
        """El estado de los tres pasos, derivado de lo que hay (ver el módulo)."""
        employee = active_employee(user)
        now = datetime.now(UTC)
        voice_required = PolicyService(self.db, self.company_id).current().voice_verification
        if employee.face_status in (FaceStatus.PENDING_REVIEW, FaceStatus.APPROVED):
            return self._finished(employee)
        pending = self._pending(employee)
        if pending is not None:
            answered = self.enrollments.voice_answers(pending.id)
            answers = len(answered)
            # El tamaño real de la sesión (las preguntas elegibles de la persona, nunca menos de las ya respondidas).
            total = len(VoiceVerificationService(self.db, self.company_id).plan(employee, answered))
            attempts_left = max(0, settings.VOICE_MAX_RETRIES_PER_QUESTION * total - pending.voice_attempts)
            if attempts_left > 0:
                return EnrollmentProgress(
                    face_status=employee.face_status,
                    photo=PhotoStep(status="done", checked_at=as_utc(pending.submitted_at)),
                    capture=CaptureStep(status="done", submitted_at=as_utc(pending.submitted_at)),
                    voice=VoiceStep(status="pending", answered=answers, total=total, attempts_left=attempts_left),
                )
            # Intentos agotados: ese registro ya no sirve (se repiten la foto y las capturas; la depuración lo borra).
            voice = VoiceStep(status="exhausted", answered=answers, total=total, attempts_left=0)
        else:
            voice = VoiceStep(status="locked" if voice_required else "not_required")
        photo = self._photo_step(employee.id, now)
        capture = CaptureStep(status="pending" if photo.status == "done" else "locked")
        return EnrollmentProgress(face_status=employee.face_status, photo=photo, capture=capture, voice=voice)

    def _pending(self, employee: Employee) -> FaceEnrollment | None:
        """El registro del empleado que espera su video (el más reciente, en validación y con la voz pendiente)."""
        latest = self.enrollments.latest_for_employee(employee.id)
        if latest is None or latest.status != EnrollmentStatus.PENDING or not latest.voice_pending:
            return None
        return latest

    def _photo_step(self, employee_id: int, now: datetime) -> PhotoStep:
        draft = self.drafts.of_employee(employee_id)
        if draft is None:
            return PhotoStep(status="pending")
        status = "done" if draft.active_at(now) else "expired"
        return PhotoStep(status=status, checked_at=as_utc(draft.checked_at), expires_at=as_utc(draft.expires_at))

    def _finished(self, employee: Employee) -> EnrollmentProgress:
        """Todo hecho (en validación o aprobado): la app muestra «En validación» o ya no muestra el registro."""
        latest = self.enrollments.latest_for_employee(employee.id)
        submitted = as_utc(latest.submitted_at) if latest is not None else None
        total = settings.VOICE_QUESTIONS_PER_SESSION
        voice = (
            VoiceStep(status="done", answered=total, total=total)
            if latest is not None and latest.voice_required
            else VoiceStep(status="not_required")
        )
        return EnrollmentProgress(
            face_status=employee.face_status,
            photo=PhotoStep(status="done", checked_at=submitted),
            capture=CaptureStep(status="done", submitted_at=submitted),
            voice=voice,
        )
