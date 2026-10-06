"""Revisión de los casos de fraude (solo el ADMIN de la plataforma; decisión D10) y su aprendizaje.

"Una persona confirma; el sistema aprende" (docs/rd/antifraude-identidad.md §3.1). Al decidir un caso:

- **Fraude confirmado**: sus huellas pHash pasan a la lista de bloqueo de su empresa y, si la misma huella ya se
  confirmó en `ATTACK_SIGNATURE_PLATFORM_COMPANIES` empresas, a la de toda la plataforma (D6: solo hashes); sus
  intentos se etiquetan FRAUD (la autocalibración deja de contarlos como personas reales: solo endurece); se olvida
  lo que el reconocimiento aprendió del empleado desde el intento; y la línea base de sus señales suma un fraude.
- **Falso positivo**: sus huellas se liberan (lista de permitidas: no vuelven a bloquear), sus intentos quedan como
  GENUINE (genuinos difíciles) y la línea base de sus señales suma un falso positivo (lo que el ADMIN ve antes de
  relajar una señal en esa empresa).
- **No concluyente / en revisión**: solo el estado (se quitan las etiquetas y, si estaba confirmado, el bloqueo).

Todo en una transacción con el caso bloqueado; el historial del caso (`fraud_case_events`) dice quién, cuándo y
por qué. La evidencia (decisión D1) solo se lee aquí, por la API, y cada consulta queda en el historial.
"""

import base64
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.core.ip_intel import ip_intel
from app.core.object_storage import StorageError
from app.models import FraudCase, FraudCaseAttempt, FraudCaseEvent, FraudCaseEventKind, FraudCaseStatus, User
from app.repositories.company_repository import CompanyRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.repositories.face_security_repository import FRAUD_LABEL, GENUINE_LABEL
from app.repositories.fraud_repository import ACTIVE_STATUSES, FraudCaseRepository
from app.repositories.risk_repository import (
    AttackSignatureRepository,
    MetricLabelRepository,
    RiskAssessmentRepository,
    RiskSignalStatRepository,
)
from app.repositories.user_repository import UserRepository
from app.schemas.common import PageParams
from app.schemas.fraud import (
    FraudCaseAttemptRead,
    FraudCaseDecision,
    FraudCaseDetail,
    FraudCaseEventRead,
    FraudCaseList,
    FraudCaseRead,
    FraudEvidenceImage,
    FraudEvidenceRead,
    FraudNetworkRead,
    RiskReasonRead,
)
from app.services import image_storage
from app.services.attack_signatures import CAPTURE_PHASH, signature_cache
from app.services.catalog_service import get_catalogs
from app.services.image_storage import FRAUD_EVIDENCE, ImageUnreadable
from app.services.shift_service import employee_ref

logger = logging.getLogger(__name__)

#: Estados a los que el ADMIN puede llevar un caso (OPEN lo pone el sistema al abrirlo).
DECISIONS = (
    FraudCaseStatus.IN_REVIEW,
    FraudCaseStatus.CONFIRMED,
    FraudCaseStatus.FALSE_POSITIVE,
    FraudCaseStatus.INCONCLUSIVE,
)
#: Etiqueta de los intentos según la decisión (las demás la quitan).
LABELS = {FraudCaseStatus.CONFIRMED: FRAUD_LABEL, FraudCaseStatus.FALSE_POSITIVE: GENUINE_LABEL}
#: Columna de la línea base que suma cada decisión.
STAT_COLUMNS = {FraudCaseStatus.CONFIRMED: "confirmed", FraudCaseStatus.FALSE_POSITIVE: "false_positive"}
#: Filtro "todos" de la bandeja (sin él, solo los casos por revisar).
ALL = "ALL"
#: Historial y evidencia que se muestran de un caso.
EVENTS_SHOWN = 100


class FraudCaseService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = FraudCaseRepository(db)

    # ------------------------------------------------------------------ bandeja

    def search(
        self, *, status: str | None, company_id: int | None, kind: str | None, page: PageParams
    ) -> FraudCaseList:
        """Por omisión, los casos por revisar; `ALL`, todos; o un estado del catálogo."""
        statuses: Sequence[str] | None = ACTIVE_STATUSES
        if status == ALL:
            statuses = None
        elif status is not None:
            if get_catalogs().get("fraud_case_statuses", status) is None:
                raise UnprocessableError(code="INVALID_CASE_STATUS", key="CASE_STATUS_FILTER_INVALID", field="status")
            statuses = (status,)
        items, total = self.repo.search(
            statuses=statuses, company_id=company_id, kind=kind, offset=page.offset, limit=page.size
        )
        return FraudCaseList.of(self._reads(items), total, page)

    def active_count(self) -> int:
        return self.repo.active_count(settings.FRAUD_CASE_MAX_ATTEMPTS * 20)

    def _reads(self, cases: Sequence[FraudCase]) -> list[FraudCaseRead]:
        """Empresas, empleados y cuentas de la página en tres consultas (sin una por caso)."""
        companies = CompanyRepository(self.db).names_by_ids({c.company_id for c in cases})
        people = self.repo.employees([c.employee_id for c in cases if c.employee_id is not None])
        emails = UserRepository(self.db).emails_by_ids(c.actor_id for c in cases if c.actor_id)
        return [
            FraudCaseRead(
                id=case.id,
                company_id=case.company_id,
                company_name=companies.get(case.company_id, ""),
                status=case.status,
                kind=case.kind,
                reason=case.reason,
                reason_name=reason_name(case.reason),
                employee=employee_ref(people[case.employee_id]) if case.employee_id in people else None,
                actor=emails.get(case.actor_id or 0),
                attempts=case.attempts,
                max_score=case.max_score,
                tier=case.tier,
                evidence=case.evidence,
                created_at=case.created_at,
                last_attempt_at=case.last_attempt_at,
                decided_by=case.decided_by,
                decided_at=case.decided_at,
                decision_note=case.decision_note,
            )
            for case in cases
        ]

    # ------------------------------------------------------------------ detalle

    def detail(self, case_id: int) -> FraudCaseDetail:
        case = self._case(case_id)
        return self._detail(case)

    def _case(self, case_id: int, *, lock: bool = False) -> FraudCase:
        case = self.repo.get(case_id, lock=lock)
        if case is None:
            raise NotFoundError(code="FRAUD_CASE_NOT_FOUND")
        return case

    def _detail(self, case: FraudCase) -> FraudCaseDetail:
        attempts = self.repo.attempts_of(case.id, settings.FRAUD_CASE_MAX_ATTEMPTS)
        events = self.repo.events_of(case.id, EVENTS_SHOWN)
        evidence = self.repo.evidence_of(case.id, settings.FRAUD_EVIDENCE_MAX_PER_CASE)
        return FraudCaseDetail(
            **self._reads([case])[0].model_dump(),
            attempts_detail=[_attempt_read(attempt) for attempt in attempts],
            events=[
                FraudCaseEventRead(
                    id=e.id,
                    created_at=e.created_at,
                    kind=e.kind,
                    actor=e.actor,
                    status_from=e.status_from,
                    status_to=e.status_to,
                    note=_event_note(e),
                )
                for e in events
            ],
            evidence_items=[
                FraudEvidenceRead(id=item.id, kind=item.kind, position=item.position, created_at=item.created_at)
                for item in evidence
            ],
        )

    def evidence(self, case_id: int, evidence_id: int, user: User) -> FraudEvidenceImage:
        """Un fotograma (descifrado en memoria, del bucket): solo el ADMIN y cada consulta queda en el historial.
        503 STORAGE_UNAVAILABLE si el bucket no responde; 404 si ya venció (se borró)."""
        case = self._case(case_id)
        item = self.repo.evidence(case.id, evidence_id)
        if item is None or item.object_name is None:
            raise NotFoundError(code="FRAUD_EVIDENCE_NOT_FOUND")
        self.db.commit()  # nada abierto mientras se espera al bucket
        try:
            data = image_storage.read(FRAUD_EVIDENCE, item) or b""
        except ImageUnreadable as exc:
            logger.error("La evidencia %s del caso %s es ilegible", evidence_id, case_id)
            raise NotFoundError(code="FRAUD_EVIDENCE_NOT_FOUND") from exc
        except StorageError as exc:
            raise image_storage.unavailable(exc) from exc
        self._event(case, FraudCaseEventKind.EVIDENCE_VIEWED, user, note=f"{item.kind} {item.position + 1}")
        self.db.commit()
        return FraudEvidenceImage(
            id=item.id,
            kind=item.kind,
            position=item.position,
            content_type=item.content_type or "image/jpeg",
            data=base64.b64encode(data).decode(),
        )

    # ------------------------------------------------------------------ revisión

    def add_note(self, case_id: int, note: str, user: User) -> FraudCaseDetail:
        case = self._case(case_id, lock=True)
        self._event(case, FraudCaseEventKind.NOTE, user, note=note)
        self.db.commit()
        return self._detail(case)

    def decide(self, case_id: int, data: FraudCaseDecision, user: User) -> FraudCaseDetail:
        if data.status not in DECISIONS:
            raise UnprocessableError(code="INVALID_CASE_STATUS", field="status")
        target = FraudCaseStatus(data.status)
        if target in STAT_COLUMNS and not data.note:
            raise UnprocessableError(code="FRAUD_NOTE_REQUIRED", field="note")
        case = self._case(case_id, lock=True)
        previous = FraudCaseStatus(case.status)
        if previous == target:
            raise ConflictError(code="FRAUD_CASE_SAME_STATUS")
        now = datetime.now(UTC)
        attempts = self.repo.attempts_of(case.id, settings.FRAUD_CASE_MAX_ATTEMPTS)
        self._event(case, FraudCaseEventKind.STATUS_CHANGED, user, note=data.note, before=previous, after=target)
        self._label(attempts, LABELS.get(target))
        self._signatures(case, attempts, previous, target, user, now)
        self._baseline(case, attempts, previous, target, now)
        if target == FraudCaseStatus.CONFIRMED and case.employee_id is not None and attempts:
            since = min(a.attempted_at for a in attempts)
            forgotten = FaceEmbeddingRepository(self.db).delete_learned_since(case.employee_id, since)
            if forgotten:
                self._event(case, FraudCaseEventKind.LEARNING_FORGOTTEN, user, note=str(forgotten))
        case.status = target
        decided = target != FraudCaseStatus.IN_REVIEW
        case.decided_at = now if decided else None
        case.decided_by_id = user.id if decided else None
        case.decided_by = user.email if decided else None
        case.decision_note = data.note if decided else None
        self.db.commit()
        signature_cache.clear()
        return self._detail(case)

    def _event(
        self,
        case: FraudCase,
        kind: FraudCaseEventKind,
        user: User | None,
        *,
        note: str | None = None,
        before: str | None = None,
        after: str | None = None,
    ) -> None:
        self.repo.add_event(
            FraudCaseEvent(
                company_id=case.company_id,
                case_id=case.id,
                created_at=datetime.now(UTC),
                kind=kind,
                actor=user.email if user else None,
                status_from=before,
                status_to=after,
                note=note,
            )
        )

    def _label(self, attempts: Sequence[FraudCaseAttempt], label: str | None) -> None:
        """Etiqueta (o limpia) las métricas y decisiones de sus intentos (la calibración y la simulación las leen)."""
        metrics = [(a.metric_id, a.attempted_at) for a in attempts if a.metric_id is not None]
        assessments = [(a.assessment_id, a.attempted_at) for a in attempts if a.assessment_id is not None]
        MetricLabelRepository(self.db).label(metrics, label)
        RiskAssessmentRepository(self.db).label(assessments, label)

    def _signatures(
        self,
        case: FraudCase,
        attempts: Sequence[FraudCaseAttempt],
        previous: FraudCaseStatus,
        target: FraudCaseStatus,
        user: User,
        now: datetime,
    ) -> None:
        """Confirmar bloquea sus huellas (y las vuelve de la plataforma si ya se confirmaron en varias empresas);
        dejar de estar confirmado o ser un falso positivo las libera."""
        values = sorted({value for attempt in attempts for value in attempt.phashes})
        blocking = target == FraudCaseStatus.CONFIRMED
        releasing = not blocking and (previous == FraudCaseStatus.CONFIRMED or target == FraudCaseStatus.FALSE_POSITIVE)
        if not values or not (blocking or releasing):
            return
        signatures = AttackSignatureRepository(self.db)
        expires = now + timedelta(days=settings.ATTACK_SIGNATURE_DAYS)
        signatures.upsert(CAPTURE_PHASH, values, case.company_id, case.id, expires, allowed=not blocking)
        counts = signatures.companies_blocking(CAPTURE_PHASH, values)
        needed = settings.ATTACK_SIGNATURE_PLATFORM_COMPANIES
        shared = [value for value in values if counts.get(value, 0) >= needed]
        # Una sentencia por cada número de empresas distinto (casi siempre una), nunca una por huella.
        by_companies: dict[int, list[str]] = {}
        for value in shared:
            by_companies.setdefault(counts[value], []).append(value)
        for companies, group in sorted(by_companies.items()):
            signatures.upsert(CAPTURE_PHASH, group, None, case.id, expires, allowed=False, companies=companies)
        signatures.release_platform(CAPTURE_PHASH, [value for value in values if value not in shared], now)
        kind = FraudCaseEventKind.SIGNATURES_BLOCKED if blocking else FraudCaseEventKind.SIGNATURES_RELEASED
        self._event(case, kind, user, note=str(len(values)))

    def _baseline(
        self,
        case: FraudCase,
        attempts: Sequence[FraudCaseAttempt],
        previous: FraudCaseStatus,
        target: FraudCaseStatus,
        now: datetime,
    ) -> None:
        """La línea base de las señales del caso: se resta la decisión anterior y se suma la nueva."""
        signals = sorted({str(reason["code"]) for attempt in attempts for reason in attempt.signals})
        stats = RiskSignalStatRepository(self.db, case.company_id)
        if previous in STAT_COLUMNS:
            stats.bump(signals, STAT_COLUMNS[previous], -1, now)
        if target in STAT_COLUMNS:
            stats.bump(signals, STAT_COLUMNS[target], 1, now)


def reason_name(code: str) -> str:
    """Nombre del motivo de un caso: el de un candado (catalog.verification_reasons) o el de una señal del motor."""
    catalogs = get_catalogs()
    row = catalogs.get("verification_reasons", code) or catalogs.get("risk_signals", code)
    return str(row["name"]) if row else code


def _signal_read(reason: dict[str, Any]) -> RiskReasonRead:
    """Una señal del intento con su nombre y su explicación del catálogo (en el idioma de quien lee)."""
    row = get_catalogs().get("risk_signals", str(reason["code"]))
    name = str(row["name"]) if row else str(reason["code"])
    return RiskReasonRead(**reason, name=name, description=row["description"] if row else None)


def _network_read(ip: str | None) -> FraudNetworkRead | None:
    """La red de la IP del intento con la base local (microsegundos, sin consultas; la IP no sale del servidor)."""
    info = ip_intel.lookup(ip)
    if info is None:
        return None
    return FraudNetworkRead(country=info.country, asn=info.asn, organization=info.organization, hosting=info.hosting)


def _attempt_read(attempt: FraudCaseAttempt) -> FraudCaseAttemptRead:
    return FraudCaseAttemptRead(
        id=attempt.id,
        attempted_at=attempt.attempted_at,
        success=attempt.success,
        reason=attempt.reason,
        score=attempt.score,
        action=attempt.action,
        signals=[_signal_read(reason) for reason in attempt.signals],
        metrics=attempt.metrics,
        signatures=len(attempt.phashes),
        camera=attempt.camera,
        ip_address=attempt.ip_address,
        network=_network_read(attempt.ip_address),
        user_agent=attempt.user_agent,
    )


def _event_note(event: FraudCaseEvent) -> str | None:
    """La nota de un evento del historial. La del sistema al abrir el caso es el CÓDIGO del motivo (dato guardado):
    se lee como el nombre del motivo en el idioma de quien lo lee (`verification_reasons`). Las demás, tal cual (la
    nota de un ADMIN, un número, la toma de evidencia vista)."""
    if event.kind == FraudCaseEventKind.OPENED and event.note:
        row = get_catalogs().get("verification_reasons", event.note)
        return row["name"] if row else event.note
    return event.note


# ---------------------------------------------------------------- mantenimiento


def purge(db: Session, now: datetime, batch: int) -> dict[str, int]:
    """La evidencia vencida sale del bucket (encolada en la misma transacción) y de la BD; los casos decididos hace
    más de su retención se borran (con sus intentos e historial). Cada tarea en lotes y falla sola."""
    removed = {"evidencia de casos de fraude": 0, "casos de fraude decididos": 0}
    repo = FraudCaseRepository(db)
    before = now - timedelta(days=settings.FRAUD_EVIDENCE_RETENTION_DAYS)
    for _ in range(settings.MAINTENANCE_MAX_BATCHES_PER_TABLE):
        ids = repo.expired_evidence(before, batch)
        if not ids:
            break
        image_storage.release(db, FRAUD_EVIDENCE, FRAUD_EVIDENCE.model.id.in_(ids))
        removed["evidencia de casos de fraude"] += repo.delete_evidence(ids)
        db.commit()
    stale = now - timedelta(days=settings.FRAUD_CASE_RETENTION_DAYS)
    for _ in range(settings.MAINTENANCE_MAX_BATCHES_PER_TABLE):
        ids = repo.stale_cases(stale, batch)
        if not ids:
            break
        image_storage.release(db, FRAUD_EVIDENCE, FRAUD_EVIDENCE.model.case_id.in_(ids))
        removed["casos de fraude decididos"] += repo.delete_cases(ids)
        db.commit()
    return removed
