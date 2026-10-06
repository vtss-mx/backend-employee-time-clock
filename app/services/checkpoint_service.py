"""Identificación de empleados en un punto de control (rol VALIDATOR, desde tableta o teléfono).

El validador identifica a CUALQUIER empleado de su empresa según su modo:
- QR: el código QR del empleado (debe ser de un empleado activo de la misma empresa).
- FACE: el rostro, con prueba de vida, contra la galería de la empresa (1:N).
- QR_OR_FACE: cualquiera de los dos.
- QR_AND_FACE: ambos; el QR dice quién es y el rostro debe ser de esa persona (1:1).
Cada intento queda en la bitácora (attendance.verification_logs) con el validador que lo hizo, y cada
identificación facial segura enseña a la galería del empleado (face_learning).

Antifraude 2b: cada identificación trae además la firma por petición del dispositivo de la sesión y, si el validador
requiere ubicación, una lectura fresca (`validator_presence`): lo obligatorio se revisa ANTES de consumir el reto o el
QR; lo que solo se mide viaja con el intento al motor de riesgo. Cada respuesta lleva el reto de la siguiente firma
(`device_nonce`).
"""

import logging
from dataclasses import replace

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, PermissionDeniedError, UnprocessableError
from app.facial_recognition import Accessory, FaceAnalysis, FacePipeline
from app.i18n import t
from app.models import Employee, User, VerificationLog, VerificationMethod
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.repositories.verification_repository import VerificationLogRepository
from app.schemas.checkpoint import CheckpointEmployee, CheckpointEvent, CheckpointEventList, CheckpointProfile
from app.schemas.common import PageParams
from app.schemas.user import UserCompanyInfo
from app.schemas.verification import FaceChallengeResponse, ValidatorAttendance, VerificationResult
from app.services import face_signals
from app.services.attempt_guard import ensure_unlocked
from app.services.attendance_service import AttendanceService
from app.services.catalog_service import get_catalogs
from app.services.client_evidence import ClientEvidence
from app.services.face_gallery import face_galleries, identify
from app.services.face_learning import Evidence, FaceLearning
from app.services.face_service import (
    SECURITY_REASONS,
    SPOOF_FLAG,
    FaceService,
    SuspiciousCapture,
    accessories_rejection,
    analyze_frames,
    blocked_migrations,
)
from app.services.identity_core import (
    FACE_SUCCESS,
    QR_SUCCESS,
    IdentityLog,
    burst_rejects,
    confirm_live,
    enforce_risk,
    ensure_frame_count,
    failed,
    issue_challenge,
    match_one,
    mean_confidence,
    reason_message,
    required_match,
    succeeded,
    take_challenge,
)
from app.services.liveness_service import LivenessResponse
from app.services.policy_service import PolicyService
from app.services.qr_service import QrService
from app.services.request_signing import ACTION_FACE, ACTION_INSPECT, ACTION_QR, digest_of
from app.services.risk_engine import Match, RiskEngine, RiskOutcome, location_context
from app.services.validator_presence import IdentificationRequest, ValidatorPresence

logger = logging.getLogger(__name__)

#: Lo que dice el validador cuando el QR solo identifica (la empresa no registra asistencia con el QR solo, D4).
QR_WITHOUT_ATTENDANCE = "QR_WITHOUT_ATTENDANCE"
#: Accesorios que impiden identificar desde la captura (la prenda de cabeza se decide al saber
#: quién es: puede tener excepción por motivos religiosos o médicos).
BLOCKING_ACCESSORIES = (Accessory.GLASSES, Accessory.MASK)


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
        #: Motivos de negocio si la última identificación quedó "en revisión".
        self.last_review: tuple[str, ...] = ()

    # ---------- Consultas ----------

    def profile(self) -> CheckpointProfile:
        return CheckpointProfile(
            id=self.validator.id,
            name=self.validator.name,
            mode=self.validator.mode,
            company=UserCompanyInfo.model_validate(self.validator.company),
            liveness_required=self.policy.liveness_required,
            qr_enabled=self.policy.qr_enabled,
            location_required=self.presence.location_required,
            device_nonce=self.presence.next_nonce(),
        )

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
        self.presence.check(request or IdentificationRequest(), ACTION_INSPECT, digest_of(qr_content.encode()))
        employee, reason = self._employee_from_qr(qr_content, hold=True)
        if employee is None:
            raise UnprocessableError(reason_message(reason), code=f"QR_{reason}")
        self.db.commit()  # el QR queda apartado: ya no sirve en ningún otro validador
        return CheckpointEmployee(
            employee_id=employee.id,
            name=employee.full_name,
            employee_number=employee.employee_number,
            device_nonce=self.presence.next_nonce(),
        )

    def identify_qr(self, qr_content: str, request: IdentificationRequest | None = None) -> VerificationResult:
        if not self._allows(VerificationMethod.QR):
            self._deny()
        presence = self.presence.check(request or IdentificationRequest(), ACTION_QR, digest_of(qr_content.encode()))
        if presence.hits:
            # Sin rostro no hay motor de riesgo: con «Solo medir» lo que no cumplió queda en el log del proceso.
            codes = ", ".join(hit.code for hit in presence.hits)
            logger.info("Identificación QR del validador %s con señales de presencia: %s", self.validator.id, codes)
        employee, reason = self._employee_from_qr(qr_content)
        self._record(employee, VerificationMethod.QR, employee is not None, None, reason)
        if employee is None:
            return self._nonce(failed(VerificationMethod.QR, reason_message(reason)))
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
        # Antes de consumir el reto: con la firma o la ubicación obligatorias, lo que no cumple se rechaza aquí.
        presence = self.presence.check(request or IdentificationRequest(), ACTION_FACE, digest_of(images[0]))
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
        try:
            challenge = take_challenge(self.db, self.user.id, liveness, camera_label, self.policy, images, client)
            frontal, headwear = self._analyze(pipeline, images)
            face_policy = self.policy.face_policy(None)
            check = confirm_live(
                self.db, self.company_id, pipeline, (challenge, liveness), frontal, self.policy, face_policy
            )
        except SuspiciousCapture as exc:
            self._record(None, method, False, None, exc.code)
            raise
        failure = check.failure
        if failure is not None:
            self._record(None, method, False, failure.score, failure.reason)
            return failed(method, failure.message)
        live = bool(check.turns)
        if qr_content:
            return self._confirm_qr_holder(pipeline, qr_content, frontal, headwear, live=live)
        return self._search_gallery(pipeline, frontal, headwear, live=live)

    # ---------- Internos ----------

    def _nonce(self, result: VerificationResult) -> VerificationResult:
        """El resultado con el reto de la siguiente firma (la app firma sin pedirlo aparte)."""
        result.device_nonce = self.presence.next_nonce()
        return result

    def _search_gallery(
        self, pipeline: FacePipeline, frontal: list[FaceAnalysis], headwear: bool, *, live: bool
    ) -> VerificationResult:
        self._migrate_missing(pipeline)
        gallery = face_galleries.get(self.db, self.company_id, pipeline.model_name)
        required = required_match(self.policy, among_all=True)
        probes = [f.embedding for f in frontal]
        found = identify(gallery, probes, required=required, margin=settings.FACE_IDENTIFY_MARGIN)
        if found.employee_id is not None and burst_rejects(
            list(gallery.matrix[gallery.labels == found.employee_id]), required
        ):
            # El video en vivo (la ráfaga) no es quien señalaron las frontales: nadie se identifica (solo endurece).
            found = replace(found, employee_id=None, reason="NO_MATCH")
        score = mean_confidence(found.similarities) if found.similarities else None
        employee = (
            EmployeeRepository(self.db, self.company_id).get_by_id(found.employee_id) if found.employee_id else None
        )
        if employee is None:
            self._record(None, VerificationMethod.FACE, False, score, found.reason or "NO_MATCH")
            return failed(VerificationMethod.FACE, reason_message(found.reason or "NO_MATCH"))
        self._ensure_headwear_allowed(employee, headwear)
        match = Match(found.similarities, required.fused)
        outcome = self._screen(employee, VerificationMethod.FACE, score, frontal, match)
        if outcome.clean:
            learning = FaceLearning(FaceService(self.db, pipeline), self.policy)
            learning.reinforce(employee, found.sample_id, Evidence(frontal, live=live, gap=found.gap))
        self._record(employee, VerificationMethod.FACE, True, score, None)
        result = succeeded(employee, VerificationMethod.FACE, FACE_SUCCESS, confidence=score)
        return self._attend(employee, result, review=outcome.review)

    def _confirm_qr_holder(
        self, pipeline: FacePipeline, qr_content: str, frontal: list[FaceAnalysis], headwear: bool, *, live: bool
    ) -> VerificationResult:
        method = VerificationMethod.QR_FACE
        PolicyService(self.db, self.company_id).ensure_qr_enabled()
        use = QrService(self.db).complete_hold(qr_content, company_id=self.company_id, actor_id=self.user.id)
        employee, reason = use.employee, use.reason
        if employee is None:
            self._record(None, method, False, None, reason)
            return failed(method, reason_message(reason))
        ensure_unlocked(self.db, self.policy, employee_id=employee.id)  # alguien con el QR de otro probando rostros
        faces = FaceService(self.db, pipeline)
        references = faces.references_for(employee)
        if not references:
            self._record(employee, method, False, None, "FACE_NOT_REGISTERED")
            return failed(method, reason_message("FACE_NOT_REGISTERED"))
        match = match_one(frontal, references, self.policy)
        if not match.matched:
            self._record(employee, method, False, match.score, "NO_MATCH")
            return failed(method, t("QR_FACE_MISMATCH"))
        self._ensure_headwear_allowed(employee, headwear)
        outcome = self._screen(
            employee, method, match.score, frontal, Match(match.similarities, required_match(self.policy).fused)
        )
        if outcome.clean:
            # El QR ya dijo quién es: la captura enseña con las mismas reglas que una verificación 1:1.
            FaceLearning(faces, self.policy).reinforce(
                employee, match.closest, Evidence(frontal, live=live), references
            )
        self._record(employee, method, True, match.score, None)
        result = succeeded(employee, method, FACE_SUCCESS, confidence=match.score)
        return self._attend(employee, result, review=outcome.review)

    def _analyze(self, pipeline: FacePipeline, images: list[bytes]) -> tuple[list[FaceAnalysis], bool]:
        """Calidad, pose y un solo rostro en cada captura; accesorios y suplantación por consenso.

        Lentes, cubrebocas y suplantación se rechazan de inmediato (422). La prenda de cabeza se
        devuelve para decidirla al saber quién es (puede tener excepción)."""
        frontal, flags = analyze_frames(pipeline, images, policy=self.policy.face_policy(None), allow_review=True)
        blocking = [a for a in BLOCKING_ACCESSORIES if a.value in flags]
        if blocking:
            raise accessories_rejection(blocking)
        if SPOOF_FLAG in flags:
            raise SuspiciousCapture("SPOOF_DETECTED")
        return frontal, Accessory.HEADWEAR.value in flags

    @staticmethod
    def _ensure_headwear_allowed(employee: Employee, headwear: bool) -> None:
        if headwear and not employee.headwear_exempt:
            raise accessories_rejection([Accessory.HEADWEAR])

    def _migrate_missing(self, pipeline: FacePipeline) -> None:
        """Empleados aprobados sin muestras del modelo actual: se generan desde su foto aprobada
        (por lotes, una sola vez por empleado), para que también se les pueda identificar.

        Los empleados del lote se cargan en UNA consulta (sin una por empleado)."""
        pending = FaceEmbeddingRepository(self.db).approved_without_model(
            self.company_id, pipeline.model_name, settings.FACE_GALLERY_MIGRATION_BATCH, exclude=blocked_migrations()
        )
        if not pending:
            return
        face_service = FaceService(self.db, pipeline)
        for employee in EmployeeRepository(self.db, self.company_id).by_ids(set(pending)).values():
            face_service.references_for(employee)

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
        mode = get_catalogs().name("validator_modes", self.validator.mode)
        raise ConflictError(code="VALIDATOR_METHOD_NOT_ALLOWED", params={"mode": mode})

    def _screen(
        self,
        employee: Employee,
        method: VerificationMethod,
        score: float | None,
        frontal: list[FaceAnalysis],
        match: Match,
    ) -> RiskOutcome:
        """El motor de riesgo sobre la identificación (ya se sabe quién es): niega o pide un paso más registrando el
        intento; si no, su decisión (aprender solo si nada pesó; "en revisión" se registra así)."""
        outcome = RiskEngine(self.db, self.company_id, self.policy).evaluate(
            employee_id=employee.id, frontal=frontal, match=match, can_step_up=self.policy.liveness_required
        )
        enforce_risk(
            self.db,
            outcome,
            actor_id=self.user.id,
            policy=self.policy,
            company_id=self.company_id,
            record=lambda reason: self._record(employee, method, False, score, reason),
        )
        self.last_review = face_signals.current().review_reasons
        return outcome

    def _attend(self, employee: Employee, result: VerificationResult, *, review: bool = False) -> VerificationResult:
        """Identificarse en un validador cuenta como entrada o salida del turno, en sitio (si tiene
        turno en ese momento): `result.attendance` dice qué registró. Con riesgo alto queda "en revisión"."""
        result.review = review
        result.attendance = AttendanceService(self.db, self.company_id).from_validator(
            employee, self.user, self.last_log, review=self.last_review if review else ()
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
