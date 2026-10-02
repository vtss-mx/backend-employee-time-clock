"""Identificación de empleados en un punto de control (rol VALIDATOR, desde tableta o teléfono).

El validador identifica a CUALQUIER empleado de su empresa según su modo:
- QR: el código QR del empleado (debe ser de un empleado activo de la misma empresa).
- FACE: el rostro, con prueba de vida, contra la galería de la empresa (1:N).
- QR_OR_FACE: cualquiera de los dos.
- QR_AND_FACE: ambos; el QR dice quién es y el rostro debe ser de esa persona (1:1).
Cada intento queda en la bitácora (attendance.verification_logs) con el validador que lo hizo.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, PermissionDeniedError, UnprocessableError
from app.facial_recognition import Accessory, FaceAnalysis, FacePipeline
from app.facial_recognition.pipeline import accessories_error
from app.models import Employee, User, ValidatorMode, VerificationLog, VerificationMethod
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.schemas.checkpoint import CheckpointEmployee, CheckpointEvent, CheckpointProfile
from app.schemas.user import UserCompanyInfo
from app.schemas.verification import FaceChallengeResponse, VerificationResult
from app.services.face_gallery import face_galleries, identify
from app.services.face_service import SPOOF_FLAG, SPOOF_MESSAGE, FaceService, analyze_frames
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
from app.services.verification_service import VerificationService

#: Métodos que permite cada modo.
MODE_METHODS: dict[ValidatorMode, frozenset[VerificationMethod]] = {
    ValidatorMode.QR: frozenset({VerificationMethod.QR}),
    ValidatorMode.FACE: frozenset({VerificationMethod.FACE}),
    ValidatorMode.QR_OR_FACE: frozenset({VerificationMethod.QR, VerificationMethod.FACE}),
    ValidatorMode.QR_AND_FACE: frozenset({VerificationMethod.QR_FACE}),
}
MODE_TEXT = {
    ValidatorMode.QR: "solo con el código QR",
    ValidatorMode.FACE: "solo con reconocimiento facial",
    ValidatorMode.QR_OR_FACE: "con QR o reconocimiento facial",
    ValidatorMode.QR_AND_FACE: "con QR y reconocimiento facial (ambos)",
}
NOT_IDENTIFIED = {
    "EMPTY_GALLERY": "Aún no hay empleados con identidad validada en esta empresa",
    "AMBIGUOUS_MATCH": "No fue posible distinguir a la persona con suficiente certeza. Intenta de nuevo con buena luz",
    "INCONSISTENT_MATCH": "Las capturas no coinciden con una sola persona. Intenta de nuevo",
}
NO_FACE_REGISTERED = "El empleado aún no tiene su rostro validado por la empresa"
#: Accesorios que impiden identificar desde la captura (la prenda de cabeza se decide al saber
#: quién es: puede tener excepción por motivos religiosos o médicos).
BLOCKING_ACCESSORIES = (Accessory.GLASSES, Accessory.MASK)


class CheckpointService:
    def __init__(self, db: Session, user: User, *, ip: str | None = None, user_agent: str | None = None) -> None:
        if user.validator is None:
            raise PermissionDeniedError("Esta cuenta no es un validador de identidad", code="VALIDATOR_REQUIRED")
        self.db = db
        self.user = user
        self.validator = user.validator
        self.company_id = user.validator.company_id
        self.policy = PolicyService(db, self.company_id).current()
        self.log = IdentityLog(db, ip=ip, user_agent=user_agent)

    # ---------- Consultas ----------

    def profile(self) -> CheckpointProfile:
        return CheckpointProfile(
            id=self.validator.id,
            name=self.validator.name,
            mode=self.validator.mode,
            company=UserCompanyInfo.model_validate(self.validator.company),
            liveness_required=self.policy.liveness_required,
            qr_enabled=self.policy.qr_enabled,
        )

    def recent(self, limit: int) -> list[CheckpointEvent]:
        """Últimas identificaciones de este validador (las más recientes primero)."""
        rows = self.db.execute(
            select(VerificationLog, Employee)
            .outerjoin(Employee, VerificationLog.employee_id == Employee.id)
            .where(VerificationLog.user_id == self.user.id)
            .order_by(VerificationLog.created_at.desc(), VerificationLog.id.desc())
            .limit(limit)
        ).all()
        return [
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
        ]

    # ---------- Identificación ----------

    def issue_challenge(self) -> FaceChallengeResponse:
        if not self._allows(VerificationMethod.FACE) and not self._allows(VerificationMethod.QR_FACE):
            self._deny()
        return issue_challenge(self.db, self.user.id, self.policy)

    def inspect_qr(self, qr_content: str) -> CheckpointEmployee:
        """Paso 1 del modo QR_AND_FACE: de quién es el QR (no se registra: el rostro decide)."""
        if not self._allows(VerificationMethod.QR_FACE):
            self._deny()
        employee, reason = self._employee_from_qr(qr_content)
        if employee is None:
            raise UnprocessableError(qr_message(reason or ""), code=f"QR_{reason}")
        return CheckpointEmployee(
            employee_id=employee.id, name=employee.full_name, employee_number=employee.employee_number
        )

    def identify_qr(self, qr_content: str) -> VerificationResult:
        if not self._allows(VerificationMethod.QR):
            self._deny()
        employee, reason = self._employee_from_qr(qr_content)
        self._record(employee, VerificationMethod.QR, employee is not None, None, reason)
        if employee is None:
            return failed(VerificationMethod.QR, qr_message(reason or ""))
        return succeeded(employee, VerificationMethod.QR, QR_SUCCESS, confidence=None)

    def identify_face(
        self,
        pipeline: FacePipeline,
        images: list[bytes],
        *,
        challenge_id: str | None,
        challenge_image: bytes | None,
        qr_content: str | None = None,
    ) -> VerificationResult:
        """Rostro 1:N (FACE / QR_OR_FACE) o QR + rostro 1:1 (QR_AND_FACE)."""
        method = VerificationMethod.QR_FACE if qr_content else VerificationMethod.FACE
        if not self._allows(method):
            if self._allows(VerificationMethod.QR_FACE):
                raise UnprocessableError("Escanea primero el código QR del empleado", code="QR_REQUIRED")
            self._deny()
        ensure_frame_count(images)
        challenge = challenge_store.require(
            self.db, self.user.id, challenge_id, challenge_image, required=self.policy.liveness_required
        )
        frontal, headwear = self._analyze(pipeline, images)
        failure = liveness_failure(pipeline, challenge, challenge_image, frontal)
        if failure is not None:
            self._record(None, method, False, failure.score, failure.reason)
            return failed(method, failure.message)
        if qr_content:
            return self._confirm_qr_holder(pipeline, qr_content, frontal, headwear)
        return self._search_gallery(pipeline, frontal, headwear)

    # ---------- Internos ----------

    def _search_gallery(
        self, pipeline: FacePipeline, frontal: list[FaceAnalysis], headwear: bool
    ) -> VerificationResult:
        self._migrate_missing(pipeline)
        gallery = face_galleries.get(self.db, self.company_id, pipeline.model_name)
        found = identify(
            gallery,
            [f.embedding for f in frontal],
            required=required_similarity(self.policy),
            margin=settings.FACE_IDENTIFY_MARGIN,
        )
        score = mean_confidence(found.similarities) if found.similarities else None
        employee = (
            EmployeeRepository(self.db, self.company_id).get_by_id(found.employee_id) if found.employee_id else None
        )
        if employee is None:
            self._record(None, VerificationMethod.FACE, False, score, found.reason or "NO_MATCH")
            return failed(VerificationMethod.FACE, NOT_IDENTIFIED.get(found.reason or "", FACE_FAILED))
        self._ensure_headwear_allowed(employee, headwear)
        self._record(employee, VerificationMethod.FACE, True, score, None)
        return succeeded(employee, VerificationMethod.FACE, FACE_SUCCESS, confidence=score)

    def _confirm_qr_holder(
        self, pipeline: FacePipeline, qr_content: str, frontal: list[FaceAnalysis], headwear: bool
    ) -> VerificationResult:
        method = VerificationMethod.QR_FACE
        employee, reason = self._employee_from_qr(qr_content)
        if employee is None:
            self._record(None, method, False, None, reason)
            return failed(method, qr_message(reason or ""))
        face_service = FaceService(self.db, pipeline)
        references = face_service.load_references(employee.id) or VerificationService(self.db).migrate_references(
            employee, face_service
        )
        if not references:
            self._record(employee, method, False, None, "FACE_NOT_REGISTERED")
            return failed(method, NO_FACE_REGISTERED)
        required = required_similarity(self.policy)
        similarities = match_references(frontal, references, required)
        score = mean_confidence(similarities)
        if min(similarities) < required:
            self._record(employee, method, False, score, "NO_MATCH")
            return failed(method, "El rostro no corresponde al dueño del código QR")
        self._ensure_headwear_allowed(employee, headwear)
        self._record(employee, method, True, score, None)
        return succeeded(employee, method, FACE_SUCCESS, confidence=score)

    def _analyze(self, pipeline: FacePipeline, images: list[bytes]) -> tuple[list[FaceAnalysis], bool]:
        """Calidad, pose y un solo rostro en cada captura; accesorios y suplantación por consenso.

        Lentes, cubrebocas y suplantación se rechazan de inmediato (422). La prenda de cabeza se
        devuelve para decidirla al saber quién es (puede tener excepción)."""
        frontal, flags = analyze_frames(pipeline, images, policy=self.policy.face_policy(None), allow_review=True)
        blocking = [a for a in BLOCKING_ACCESSORIES if a.value in flags]
        if blocking:
            error = accessories_error(blocking)
            raise UnprocessableError(error.message, code=error.code, details=error.details)
        if SPOOF_FLAG in flags:
            raise UnprocessableError(SPOOF_MESSAGE, code="SPOOF_DETECTED")
        return frontal, Accessory.HEADWEAR.value in flags

    @staticmethod
    def _ensure_headwear_allowed(employee: Employee, headwear: bool) -> None:
        if headwear and not employee.headwear_exempt:
            error = accessories_error([Accessory.HEADWEAR])
            raise UnprocessableError(error.message, code=error.code, details=error.details)

    def _migrate_missing(self, pipeline: FacePipeline) -> None:
        """Empleados aprobados sin muestras del modelo actual: se generan desde su foto aprobada
        (por lotes, una sola vez por empleado), para que también se les pueda identificar."""
        pending = FaceEmbeddingRepository(self.db).approved_without_model(
            self.company_id, pipeline.model_name, settings.FACE_GALLERY_MIGRATION_BATCH
        )
        if not pending:
            return
        employees = EmployeeRepository(self.db, self.company_id)
        face_service = FaceService(self.db, pipeline)
        verification = VerificationService(self.db)
        for employee_id in pending:
            employee = employees.get_by_id(employee_id)
            if employee is not None:
                verification.migrate_references(employee, face_service)

    def _employee_from_qr(self, qr_content: str) -> tuple[Employee | None, str | None]:
        """Empleado activo de ESTA empresa dueño del QR, o el motivo del rechazo."""
        PolicyService(self.db, self.company_id).ensure_qr_enabled()
        lookup = QrService(self.db).lookup(qr_content)
        if lookup.reason is not None or lookup.qr is None:
            return None, lookup.reason or "NOT_FOUND"
        employee = lookup.qr.employee
        if employee.company_id != self.company_id:
            return None, "OTHER_COMPANY"  # se informa como "QR no reconocido"
        if not employee.active:
            return None, "EMPLOYEE_INACTIVE"
        return employee, None

    def _allows(self, method: VerificationMethod) -> bool:
        return method in MODE_METHODS[self.validator.mode]

    def _deny(self) -> None:
        raise ConflictError(
            f"Este validador identifica {MODE_TEXT[self.validator.mode]}", code="VALIDATOR_METHOD_NOT_ALLOWED"
        )

    def _record(
        self,
        employee: Employee | None,
        method: VerificationMethod,
        success: bool,
        score: float | None,
        reason: str | None,
    ) -> None:
        self.log.record(
            company_id=self.company_id,
            employee_id=employee.id if employee else None,
            actor_id=self.user.id,
            method=method,
            success=success,
            score=score,
            reason=reason,
        )
