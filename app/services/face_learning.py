"""Reconocimiento facial evolutivo: la galería de cada empleado mejora con el uso.

Cada empleado empieza con las muestras de su registro aprobado: el ANCLA (las validó una persona y
nunca se reemplazan). Cada identificación exitosa y segura puede enseñarle al sistema cómo luce hoy
esa persona (otra luz, otra cámara, barba, peinado, el paso de los años) agregando una muestra
APRENDIDA. Con el uso la confianza sube y bajan los rechazos de la persona correcta, en las
verificaciones 1:1, en QR + rostro y en la identificación 1:N del validador.

Aprender de una identificación equivocada envenenaría la galería (la siguiente vez el impostor
entraría más fácil), así que una captura solo enseña si cumple todas estas reglas:

1. Permiso: la empresa tiene activo el aprendizaje (`adaptive_learning`) y la captura superó el
   reto de prueba de vida (sin reto no se aprende: una foto no debe poder enseñar nada).
2. Certeza de quién es: en el validador (1:N) la persona superó a la segunda más parecida por
   FACE_LEARNING_IDENTIFY_MARGIN. En 1:1 y con QR + rostro ya se sabía quién era.
3. Holgura: CADA captura superó lo que exige la empresa por FACE_LEARNING_MARGIN (no se aprende de
   identificaciones que pasaron justas).
4. Ancla: la captura alcanza por sí sola lo que exige la empresa contra las muestras APROBADAS. Una
   muestra aprendida nunca enseña a otra: la galería no puede desviarse poco a poco hacia otra cara.
5. Novedad: no es casi idéntica a una muestra que ya se tiene (FACE_LEARNING_REDUNDANCY).
6. Ritmo: como máximo una muestra por empleado cada FACE_LEARNING_INTERVAL_HOURS (variedad de días
   y condiciones, y pocas escrituras).

Selección: cada identificación exitosa suma utilidad a la muestra que más se pareció (`matches`,
`last_matched_at`), sea aprobada o aprendida. Con los lugares llenos (FACE_LEARNING_MAX_SAMPLES) la
muestra aprendida que lleva más tiempo sin servir deja su lugar a la nueva: sobreviven las que
ayudan a reconocer. Todo esto lo administra el ADMIN de la plataforma (la empresa no ve ni configura
el aprendizaje): lo activa en la política de cada empresa, consulta su evolución y olvida lo
aprendido de un empleado si duda de alguna identificación (`CompanyService.forget_learned_face`):
vuelve a su registro aprobado.
"""

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.facial_recognition import FaceAnalysis
from app.facial_recognition.matcher import similarity_matrix
from app.models import Employee
from app.repositories.face_repository import FaceEmbeddingRepository
from app.schemas.face import FaceLearningSummary
from app.services.face_service import FaceService, Reference
from app.services.identity_core import required_similarity
from app.services.policy_service import PolicyService, PolicySnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Evidence:
    """Una identificación exitosa y lo que la respalda."""

    frontal: Sequence[FaceAnalysis]
    #: Superó el reto de prueba de vida (se analizaron sus giros).
    live: bool
    #: 1:N: ventaja sobre la segunda persona más parecida. None = ya se sabía quién era (1:1, QR).
    gap: float | None = None


class FaceLearning:
    """Lo que aprende la galería de los empleados de UNA empresa (con su política vigente)."""

    def __init__(self, faces: FaceService, policy: PolicySnapshot) -> None:
        self.faces = faces
        self.policy = policy

    def reinforce(
        self,
        employee: Employee,
        closest: int | None,
        evidence: Evidence,
        references: Sequence[Reference] | None = None,
    ) -> bool:
        """Tras una identificación exitosa: suma utilidad a la muestra que la decidió (`closest`) y
        aprende de la captura si es segura. `references` = las muestras ya cargadas (si no, se leen
        solo cuando hace falta). Escribe en la transacción en curso: la cierra quien registra el
        intento en la bitácora. Devuelve si se aprendió una muestra."""
        now = datetime.now(UTC)
        if closest is not None:
            self.faces.repo.credit(closest, now)
        if not self._allowed(evidence):
            return False
        try:  # aprender es de mejor esfuerzo: nunca convierte en falla una identificación ya exitosa
            current = list(references) if references is not None else self.faces.references_for(employee)
            candidate = self._candidate(current, evidence, now)
        except Exception:
            logger.exception("No se pudo evaluar el aprendizaje del rostro del empleado %s", employee.id)
            return False
        if candidate is None:
            return False
        self._make_room(current, closest, now)
        self.faces.store(employee.id, [candidate], learned=True)
        logger.info("Rostro del empleado %s: muestra aprendida de una identificación segura", employee.id)
        return True

    # ---------- Reglas ----------

    def _allowed(self, evidence: Evidence) -> bool:
        """Permiso y certeza de quién es (reglas 1 y 2): sin ellos ni siquiera se leen las muestras."""
        if not (self.policy.adaptive_learning and evidence.live and settings.FACE_LEARNING_MAX_SAMPLES > 0):
            return False
        return evidence.gap is None or evidence.gap >= settings.FACE_LEARNING_IDENTIFY_MARGIN

    def _candidate(self, references: Sequence[Reference], evidence: Evidence, now: datetime) -> FaceAnalysis | None:
        """La captura (la de mejor calidad) que enseña algo, o None (reglas 3 a 6)."""
        anchors = [i for i, r in enumerate(references) if not r.learned]
        if not anchors or self._learned_recently(references, now):
            return None
        required = required_similarity(self.policy)
        scores = similarity_matrix([f.embedding for f in evidence.frontal], [r.vector for r in references])
        if float(scores.max(axis=1).min()) < required + settings.FACE_LEARNING_MARGIN:
            return None
        best = max(range(len(evidence.frontal)), key=lambda i: evidence.frontal[i].quality_score)
        if float(scores[best, anchors].max()) < required:
            return None
        if float(scores[best].max()) >= settings.FACE_LEARNING_REDUNDANCY:
            return None
        return evidence.frontal[best]

    @staticmethod
    def _learned_recently(references: Sequence[Reference], now: datetime) -> bool:
        pause = timedelta(hours=settings.FACE_LEARNING_INTERVAL_HOURS)
        return any(r.learned and now - as_utc(r.created_at) < pause for r in references)

    def _make_room(self, references: Sequence[Reference], closest: int | None, now: datetime) -> None:
        """Con los lugares llenos, sale la muestra aprendida que lleva más tiempo sin servir (la que
        acaba de decidir esta identificación cuenta como recién usada)."""

        def last_useful(reference: Reference) -> datetime:
            return now if reference.id == closest else as_utc(reference.last_matched_at or reference.created_at)

        learned = sorted((r for r in references if r.learned), key=last_useful)
        excess = len(learned) - settings.FACE_LEARNING_MAX_SAMPLES + 1
        if excess > 0:
            self.faces.repo.delete_ids([r.id for r in learned[:excess]])


def learning_summary(db: Session, company_id: int) -> FaceLearningSummary:
    """Cómo evoluciona el reconocimiento de la empresa (tablero de COMPANY)."""
    totals = FaceEmbeddingRepository(db).learning_totals(company_id)
    return FaceLearningSummary(enabled=PolicyService(db, company_id).current().adaptive_learning, **asdict(totals))
