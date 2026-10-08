"""Identificación de empleados en un punto de control (rol VALIDATOR, desde tableta o teléfono).

El validador identifica a CUALQUIER empleado de su empresa según su modo:
- QR: el código QR del empleado (debe ser de un empleado activo de la misma empresa).
- FACE: el rostro, con prueba de vida, contra la galería de la empresa (1:N).
- QR_OR_FACE: cualquiera de los dos.
- QR_AND_FACE: ambos; el QR dice quién es y el rostro debe ser de esa persona (1:1).
Cada intento queda en la bitácora (attendance.verification_logs) con el validador que lo hizo, y cada
identificación facial segura enseña a la galería del empleado (face_learning).

El 1:N por rostro (los candados de la toma, la búsqueda en la galería y el motor de riesgo) vive en
`face_identification.GallerySearch`, el mismo de la API pública de verificación (SDK móviles).

Antifraude 2b: cada identificación trae además la firma por petición del dispositivo de la sesión y, si el validador
requiere ubicación, una lectura fresca (`validator_presence`): lo obligatorio se revisa ANTES de consumir el reto o el
QR; lo que solo se mide viaja con el intento al motor de riesgo. Cada respuesta lleva el reto de la siguiente firma
(`device_nonce`).
"""

import logging
from dataclasses import replace

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, PermissionDeniedError, UnprocessableError
from app.facial_recognition import FacePipeline
from app.i18n import Text, t
from app.models import Employee, SignalMode, User, VerificationLog, VerificationMethod
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.verification_repository import VerificationLogRepository
from app.schemas.avatar import employee_avatar
from app.schemas.checkpoint import CheckpointEmployee, CheckpointEvent, CheckpointEventList, CheckpointProfile
from app.schemas.common import PageParams
from app.schemas.user import UserCompanyInfo
from app.schemas.verification import FaceChallengeResponse, ValidatorAttendance, VerificationResult
from app.services.attempt_guard import ensure_unlocked
from app.services.attendance_service import AttendanceService
from app.services.catalog_service import catalog_name_text, get_catalogs, reason_text
from app.services.client_evidence import ClientEvidence
from app.services.face_identification import GallerySearch, Take
from app.services.face_learning import Evidence, FaceLearning
from app.services.face_service import SECURITY_REASONS, FaceService
from app.services.identity_core import (
    FACE_SUCCESS,
    QR_SUCCESS,
    IdentityLog,
    ensure_frame_count,
    ensure_verification_location,
    failed,
    issue_challenge,
    match_one,
    required_match,
    succeeded,
)
from app.services.liveness_service import LivenessResponse
from app.services.policy_service import PolicyService
from app.services.qr_service import QrService
from app.services.request_signing import ACTION_FACE, ACTION_INSPECT, ACTION_QR, digest_of
from app.services.risk_engine import Match, location_context
from app.services.validator_presence import IdentificationRequest, ValidatorPresence

logger = logging.getLogger(__name__)

#: Lo que dice el validador cuando el QR solo identifica (la empresa no registra asistencia con el QR solo, D4).
QR_WITHOUT_ATTENDANCE = "QR_WITHOUT_ATTENDANCE"


class CheckpointService:
    def __init__(
        self,
        db: Session,
        user: User,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> None:
        if user.validator is None:
            raise PermissionDeniedError(code="VALIDATOR_REQUIRED")
        self.db = db
        self.user = user
        self.validator = user.validator
        self.company_id = user.validator.company_id
        self.policy = PolicyService(db, self.company_id).current()
        self.log = IdentityLog(db, ip=ip, user_agent=user_agent)
        #: La firma por petición y la ubicación de cada identificación (antifraude 2b).
        self.presence = ValidatorPresence(db, self.validator, (session_id, user.session_device_key), self.policy)
        #: El último intento registrado (la asistencia lo enlaza como evidencia).
        self.last_log: VerificationLog | None = None
        #: El 1:N de la empresa (el mismo de la API pública de verificación) operado por este validador.
        self.gallery = GallerySearch(db, self.company_id, self.policy, actor=user.id, record=self._record)

    # ---------- Consultas ----------

    def profile(self) -> CheckpointProfile:
        return CheckpointProfile(
            id=self.validator.id,
            name=self.validator.name,
            mode=self.validator.mode,
            company=UserCompanyInfo.model_validate(self.validator.company),
            liveness_required=self.policy.liveness_required,
            qr_enabled=self.policy.qr_enabled,
            location_required=self._needs_location(),
            device_nonce=self.presence.next_nonce(),
        )

    def _needs_location(self) -> bool:
        """La app manda la ubicación en cada identificación cuando el validador la requiere (presencia 2b) O cuando la
        empresa registra la ubicación de las verificaciones (`verification_location` OBSERVE/ENFORCE): así la app
        (que decide por `location_required`) la envía y la empresa la ve en el mapa."""
        return self.presence.location_required or self.policy.verification_location != SignalMode.OFF

    def recent(self, page: PageParams) -> CheckpointEventList:
        """Identificaciones de este validador, paginadas (las más recientes primero)."""
        logs, total = VerificationLogRepository(self.db).page_for_actor(
            self.user.id, offset=page.offset, limit=page.size
        )
        people = EmployeeRepository(self.db, self.company_id).by_ids(
            {log.employee_id for log in logs if log.employee_id is not None}
        )
        rows = [(log, people.get(log.employee_id) if log.employee_id else None) for log in logs]
        return CheckpointEventList.of(
            [
                CheckpointEvent(
                    id=log.id,
                    created_at=log.created_at,
                    method=log.method,
                    success=log.success,
                    reason=log.reason,
                    confidence=log.score if log.method != VerificationMethod.QR else None,
                    employee_name=employee.full_name if employee and log.success else None,
                    employee_number=employee.employee_number if employee and log.success else None,
                    avatar=employee_avatar(employee) if employee and log.success else None,
                )
                for log, employee in rows
            ],
            total,
            page,
        )

    # ---------- Identificación ----------

    def issue_challenge(self) -> FaceChallengeResponse:
        if not self._allows(VerificationMethod.FACE) and not self._allows(VerificationMethod.QR_FACE):
            self._deny()
        return issue_challenge(self.db, self.user.id, self.policy, self.company_id)

    def inspect_qr(self, qr_content: str, request: IdentificationRequest | None = None) -> CheckpointEmployee:
        """Paso 1 del modo QR_AND_FACE: de quién es el QR. El QR queda usado (apartado para este
        validador) y el rostro lo completa; no se registra en la bitácora: el rostro decide (también lo que se midió
        de la firma y la ubicación de este paso: el del rostro vuelve a traerlas)."""
        if not self._allows(VerificationMethod.QR_FACE):
            self._deny()
        req = request or IdentificationRequest()
        # La ubicación de la verificación en ENFORCE se exige antes de apartar el QR (el rostro vuelve a traerla).
        ensure_verification_location(self.policy, req.location)
        self.presence.check(req, ACTION_INSPECT, digest_of(qr_content.encode()))
        employee, reason = self._employee_from_qr(qr_content, hold=True)
        if employee is None:
            raise UnprocessableError(reason_text(reason), code=f"QR_{reason}")
        self.db.commit()  # el QR queda apartado: ya no sirve en ningún otro validador
        return CheckpointEmployee(
            employee_id=employee.id,
            name=employee.full_name,
            employee_number=employee.employee_number,
            avatar=employee_avatar(employee),
            device_nonce=self.presence.next_nonce(),
        )

    def identify_qr(self, qr_content: str, request: IdentificationRequest | None = None) -> VerificationResult:
        if not self._allows(VerificationMethod.QR):
            self._deny()
        req = request or IdentificationRequest()
        self.log.location = req.location  # dónde se hizo (mapa de «Verificaciones»)
        ensure_verification_location(self.policy, req.location)
        presence = self.presence.check(req, ACTION_QR, digest_of(qr_content.encode()))
        if presence.hits:
            # Sin rostro no hay motor de riesgo: con «Solo medir» lo que no cumplió queda en el log del proceso.
            codes = ", ".join(hit.code for hit in presence.hits)
            logger.info("Identificación QR del validador %s con señales de presencia: %s", self.validator.id, codes)
        employee, reason = self._employee_from_qr(qr_content)
        self._record(employee, VerificationMethod.QR, employee is not None, None, reason)
        if employee is None:
            return self._nonce(failed(VerificationMethod.QR, reason_text(reason)))
        result = succeeded(employee, VerificationMethod.QR, QR_SUCCESS, confidence=None)
        if not self.policy.qr_only_attendance:
            # Decisión D4: un QR se puede reenviar a un cómplice; sin rostro identifica, pero no registra asistencia.
            result.attendance = ValidatorAttendance(message=t(QR_WITHOUT_ATTENDANCE))
            return self._nonce(result)
        return self._nonce(self._attend(employee, result))

    def identify_face(
        self,
        pipeline: FacePipeline,
        images: list[bytes],
        *,
        liveness: LivenessResponse,
        qr_content: str | None = None,
        camera_label: str | None = None,
        client: ClientEvidence | None = None,
        request: IdentificationRequest | None = None,
    ) -> VerificationResult:
        """Rostro 1:N (FACE / QR_OR_FACE) o QR + rostro 1:1 (QR_AND_FACE). `client`: lo que informó la app (telemetría
        de la toma); `request`: la firma por petición y la ubicación del dispositivo (antifraude 2b).

        Un validador con demasiados intentos sospechosos seguidos se bloquea temporalmente (429
        FACE_LOCKED); con QR + rostro, también el empleado del QR tras varios fallos.
        """
        method = VerificationMethod.QR_FACE if qr_content else VerificationMethod.FACE
        if not self._allows(method):
            if self._allows(VerificationMethod.QR_FACE):
                raise UnprocessableError(code="QR_REQUIRED")
            self._deny()
        ensure_frame_count(images)
        req = request or IdentificationRequest()
        self.log.location = req.location  # dónde se hizo (mapa de «Verificaciones»)
        # Antes de consumir el reto: con la firma, la presencia del validador o la ubicación de la verificación
        # obligatorias, lo que no cumple se rechaza aquí.
        ensure_verification_location(self.policy, req.location)
        presence = self.presence.check(req, ACTION_FACE, digest_of(images[0]))
        evidence = replace(client or ClientEvidence(), checks=presence.hits)
        if presence.evidence is None:
            return self._nonce(self._identify_face(pipeline, images, liveness, qr_content, camera_label, evidence))
        with location_context(presence.evidence):  # las señales del lugar y de la red para el motor de riesgo
            return self._nonce(self._identify_face(pipeline, images, liveness, qr_content, camera_label, evidence))

    def _identify_face(
        self,
        pipeline: FacePipeline,
        images: list[bytes],
        liveness: LivenessResponse,
        qr_content: str | None,
        camera_label: str | None,
        client: ClientEvidence,
    ) -> VerificationResult:
        method = VerificationMethod.QR_FACE if qr_content else VerificationMethod.FACE
        ensure_unlocked(self.db, self.policy, actor_id=self.user.id, reasons=SECURITY_REASONS)
        take = self.gallery.capture(pipeline, images, liveness, camera_label, client, method=method)
        if isinstance(take, VerificationResult):
            return take
        if qr_content:
            return self._confirm_qr_holder(pipeline, qr_content, take)
        result, employee = self.gallery.search(pipeline, take)
        return result if employee is None else self._attend(employee, result, review=result.review)

    # ---------- Internos ----------

    def _nonce(self, result: VerificationResult) -> VerificationResult:
        """El resultado con el reto de la siguiente firma (la app firma sin pedirlo aparte)."""
        result.device_nonce = self.presence.next_nonce()
        return result

    def _confirm_qr_holder(self, pipeline: FacePipeline, qr_content: str, take: Take) -> VerificationResult:
        method = VerificationMethod.QR_FACE
        frontal = take.frontal
        PolicyService(self.db, self.company_id).ensure_qr_enabled()
        use = QrService(self.db).complete_hold(qr_content, company_id=self.company_id, actor_id=self.user.id)
        employee, reason = use.employee, use.reason
        if employee is None:
            self._record(None, method, False, None, reason)
            return failed(method, reason_text(reason))
        ensure_unlocked(self.db, self.policy, employee_id=employee.id)  # alguien con el QR de otro probando rostros
        faces = FaceService(self.db, pipeline)
        references = faces.references_for(employee)
        if not references:
            self._record(employee, method, False, None, "FACE_NOT_REGISTERED")
            return failed(method, reason_text("FACE_NOT_REGISTERED"))
        match = match_one(frontal, references, self.policy)
        if not match.matched:
            self._record(employee, method, False, match.score, "NO_MATCH")
            return failed(method, Text("QR_FACE_MISMATCH"))
        self.gallery.ensure_headwear_allowed(employee, take.headwear)
        outcome = self.gallery.screen(
            employee, method, match.score, frontal, Match(match.similarities, required_match(self.policy).fused)
        )
        if outcome.clean:
            # El QR ya dijo quién es: la captura enseña con las mismas reglas que una verificación 1:1.
            FaceLearning(faces, self.policy).reinforce(
                employee, match.closest, Evidence(frontal, live=take.live), references
            )
        self._record(employee, method, True, match.score, None)
        result = succeeded(employee, method, FACE_SUCCESS, confidence=match.score)
        return self._attend(employee, result, review=outcome.review)

    def _employee_from_qr(self, qr_content: str, *, hold: bool = False) -> tuple[Employee | None, str | None]:
        """Empleado activo de ESTA empresa dueño del QR (que queda usado para siempre), o el motivo
        del rechazo."""
        PolicyService(self.db, self.company_id).ensure_qr_enabled()
        use = QrService(self.db).use(qr_content, company_id=self.company_id, actor_id=self.user.id, hold=hold)
        return use.employee, use.reason

    def _allows(self, method: VerificationMethod) -> bool:
        # Métodos permitidos por modo: catalog.validator_mode_methods.
        return method.value in get_catalogs().mode_methods.get(self.validator.mode.value, ())

    def _deny(self) -> None:
        mode = catalog_name_text("validator_modes", self.validator.mode)
        raise ConflictError(code="VALIDATOR_METHOD_NOT_ALLOWED", params={"mode": mode})

    def _attend(self, employee: Employee, result: VerificationResult, *, review: bool = False) -> VerificationResult:
        """Identificarse en un validador cuenta como entrada o salida del turno, en sitio (si tiene
        turno en ese momento): `result.attendance` dice qué registró. Con riesgo alto queda "en revisión"."""
        result.review = review
        result.attendance = AttendanceService(self.db, self.company_id).from_validator(
            employee, self.user, self.last_log, review=self.gallery.last_review if review else ()
        )
        return result

    def _record(
        self,
        employee: Employee | None,
        method: VerificationMethod,
        success: bool,
        score: float | None,
        reason: str | None,
    ) -> None:
        self.last_log = self.log.record(
            company_id=self.company_id,
            employee_id=employee.id if employee else None,
            actor_id=self.user.id,
            method=method,
            success=success,
            score=score,
            reason=reason,
        )
