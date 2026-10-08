"""Identificación 1:N por rostro contra la galería de UNA empresa.

La usan el punto de control (`CheckpointService`, el validador) y la API pública de verificación
(`ApiVerificationService`, la aplicación móvil de la empresa con el SDK): las mismas reglas en un solo lugar.

- Las capturas frontales: calidad, pose y un solo rostro; lentes, cubrebocas y suplantación se rechazan de inmediato
  (422); la prenda de cabeza se decide al saber quién es (puede tener excepción por motivos religiosos o médicos).
- La búsqueda: todas las capturas deben señalar a la misma persona, alcanzar la confianza de identificación de la
  empresa (nunca menor que la de verificar) y superar a la segunda más parecida por `FACE_IDENTIFY_MARGIN`; el consenso
  de la ráfaga solo endurece (`burst_rejects`).
- El motor de riesgo después de saber quién es (niega o pide un paso más registrando el intento) y, si nada pesó, la
  galería del empleado aprende de la captura (`face_learning`).

Quién opera la cámara y cómo se registra cada intento lo pone quien la usa (`record`): la bitácora del validador o la
del dispositivo de la API.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from app.core.config import settings
from app.facial_recognition import Accessory, FaceAnalysis, FacePipeline
from app.models import Employee, VerificationMethod
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.schemas.verification import VerificationResult
from app.services import face_signals
from app.services.catalog_service import reason_text
from app.services.client_evidence import ClientEvidence
from app.services.face_gallery import face_galleries, identify
from app.services.face_learning import Evidence, FaceLearning
from app.services.face_service import (
    SPOOF_FLAG,
    FaceService,
    SuspiciousCapture,
    accessories_rejection,
    analyze_frames,
    blocked_migrations,
)
from app.services.identity_core import (
    FACE_SUCCESS,
    burst_rejects,
    confirm_live,
    enforce_risk,
    failed,
    mean_confidence,
    required_match,
    succeeded,
    take_challenge,
)
from app.services.liveness_service import ChallengeOwner, LivenessResponse
from app.services.policy_service import PolicySnapshot
from app.services.risk_engine import Match, RiskEngine, RiskOutcome

#: Accesorios que impiden identificar desde la captura (la prenda de cabeza se decide al saber
#: quién es: puede tener excepción por motivos religiosos o médicos).
BLOCKING_ACCESSORIES = (Accessory.GLASSES, Accessory.MASK)

#: Anota un intento en la bitácora: (empleado o None, método, éxito, confianza, motivo).
type Recorder = Callable[[Employee | None, VerificationMethod, bool, float | None, str | None], None]


@dataclass(frozen=True)
class Take:
    """Una toma que pasó los candados (cámara real, reto, prueba de vida, suplantación y toma única): sus frontales
    analizadas, si alguna trae prenda de cabeza (se decide al saber quién es) y si hubo movimientos (`live`)."""

    frontal: list[FaceAnalysis]
    headwear: bool
    live: bool


class GallerySearch:
    """El 1:N de una empresa con quien opera la cámara (`actor`: la cuenta del validador o un dispositivo de la API) y
    cómo se registra cada intento (`record`)."""

    def __init__(
        self, db: Session, company_id: int, policy: PolicySnapshot, *, actor: ChallengeOwner, record: Recorder
    ) -> None:
        self.db = db
        self.company_id = company_id
        self.policy = policy
        self.actor = actor
        self.record = record
        #: Motivos de negocio si la última identificación quedó "en revisión".
        self.last_review: tuple[str, ...] = ()

    def capture(
        self,
        pipeline: FacePipeline,
        images: list[bytes],
        liveness: LivenessResponse,
        camera_label: str | None,
        client: ClientEvidence,
        *,
        method: VerificationMethod,
    ) -> Take | VerificationResult:
        """Los candados de la toma ANTES de buscar a nadie (`identity_core`: `take_challenge` y `confirm_live`). Un
        intento sospechoso se registra y sigue su camino (`SuspiciousCapture`); una prueba de vida no superada se
        registra y es el resultado."""
        try:
            challenge = take_challenge(self.db, self.actor, liveness, camera_label, self.policy, images, client)
            frontal, headwear = self.analyze(pipeline, images)
            face_policy = self.policy.face_policy(None)
            check = confirm_live(
                self.db, self.company_id, pipeline, (challenge, liveness), frontal, self.policy, face_policy
            )
        except SuspiciousCapture as exc:
            self.record(None, method, False, None, exc.code)
            raise
        failure = check.failure
        if failure is not None:
            self.record(None, method, False, failure.score, failure.reason)
            return failed(method, failure.text)
        return Take(frontal, headwear, live=bool(check.turns))

    def analyze(self, pipeline: FacePipeline, images: list[bytes]) -> tuple[list[FaceAnalysis], bool]:
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
    def ensure_headwear_allowed(employee: Employee, headwear: bool) -> None:
        if headwear and not employee.headwear_exempt:
            raise accessories_rejection([Accessory.HEADWEAR])

    def search(
        self, pipeline: FacePipeline, take: Take, *, method: VerificationMethod = VerificationMethod.FACE
    ) -> tuple[VerificationResult, Employee | None]:
        """Quién es entre los empleados con identidad validada: el resultado (registrado con `method`) y el empleado
        identificado (None si nadie). Con riesgo, niega o pide un paso más (`enforce_risk`); "en revisión" queda en
        `result.review` y sus motivos en `last_review`."""
        frontal = take.frontal
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
            self.record(None, method, False, score, found.reason or "NO_MATCH")
            return failed(method, reason_text(found.reason or "NO_MATCH")), None
        self.ensure_headwear_allowed(employee, take.headwear)
        outcome = self.screen(employee, method, score, frontal, Match(found.similarities, required.fused))
        if outcome.clean:
            learning = FaceLearning(FaceService(self.db, pipeline), self.policy)
            learning.reinforce(employee, found.sample_id, Evidence(frontal, live=take.live, gap=found.gap))
        self.record(employee, method, True, score, None)
        result = succeeded(employee, method, FACE_SUCCESS, confidence=score)
        result.review = outcome.review
        return result, employee

    def screen(
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
            actor_id=self.actor,
            policy=self.policy,
            company_id=self.company_id,
            record=lambda reason: self.record(employee, method, False, score, reason),
        )
        self.last_review = face_signals.current().review_reasons
        return outcome

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
