"""Consultas de los casos de fraude, sus intentos, su historial y su evidencia (sin reglas de negocio ni `commit`).

Los casos se escriben desde un intento facial (alcance de su empresa) y los revisa el ADMIN (alcance de la
plataforma): cada consulta filtra por lo que su alcance permite y va por un índice de `app/models/fraud.py`."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Row, case, delete, func, select, text, update
from sqlalchemy.orm import Session, lazyload

from app.core.soft_delete import with_deleted
from app.models import Employee, FraudCase, FraudCaseAttempt, FraudCaseEvent, FraudEvidence
from app.models.fraud import ACTIVE_CASE
from app.repositories.aggregates import LOG_COUNT_CAP, affected_rows, dialect_insert, paginate

#: Estados de un caso que todavía esperan revisión.
ACTIVE_STATUSES = ("OPEN", "IN_REVIEW")


class FraudCaseRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------ desde un intento

    def observe(self, values: dict[str, Any]) -> Row[int, int, int]:
        """Abre el caso activo del sujeto o le suma el intento, en UNA sentencia atómica (dos intentos simultáneos
        del mismo sujeto nunca abren dos casos: índice único parcial de los activos). Devuelve (id, intentos,
        evidencia): con 1 intento es un caso nuevo."""
        stmt = dialect_insert(self.db, FraudCase).values(attempts=1, status="OPEN", **values)
        higher = stmt.excluded.max_score > func.coalesce(FraudCase.max_score, -1)
        stmt = stmt.on_conflict_do_update(
            index_elements=["company_id", "subject"],
            index_where=text(ACTIVE_CASE),
            set_={
                "attempts": FraudCase.attempts + 1,
                "last_attempt_at": stmt.excluded.last_attempt_at,
                "max_score": case((higher, stmt.excluded.max_score), else_=FraudCase.max_score),
                "tier": case((higher, stmt.excluded.tier), else_=FraudCase.tier),
            },
        ).returning(FraudCase.id, FraudCase.attempts, FraudCase.evidence)
        row: Row[int, int, int] = self.db.execute(stmt).one()
        return row

    def add_attempt(self, attempt: FraudCaseAttempt) -> None:
        self.db.add(attempt)

    def add_event(self, event: FraudCaseEvent) -> None:
        self.db.add(event)

    def add_evidence(self, rows: Sequence[FraudEvidence]) -> None:
        self.db.add_all(rows)

    def count_evidence(self, case_id: int, added: int) -> None:
        affected_rows(
            self.db, update(FraudCase).where(FraudCase.id == case_id).values(evidence=FraudCase.evidence + added)
        )

    # ------------------------------------------------------------------ revisión del ADMIN (plataforma)

    def get(self, case_id: int, *, lock: bool = False) -> FraudCase | None:
        stmt = select(FraudCase).where(FraudCase.id == case_id)
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return self.db.scalar(stmt)

    def search(
        self, *, statuses: Sequence[str] | None, company_id: int | None, kind: str | None, offset: int, limit: int
    ) -> tuple[list[FraudCase], int]:
        """La bandeja, lo más reciente primero: por estado (índice status + last_attempt_at) o por empresa (índice
        company_id + last_attempt_at); el total con tope."""
        stmt = select(FraudCase)
        if statuses:
            stmt = stmt.where(FraudCase.status.in_(statuses))
        if company_id is not None:
            stmt = stmt.where(FraudCase.company_id == company_id)
        if kind is not None:
            stmt = stmt.where(FraudCase.kind == kind)
        order = (FraudCase.last_attempt_at.desc(), FraudCase.id.desc())
        return paginate(self.db, stmt, order, offset=offset, limit=limit, count_cap=LOG_COUNT_CAP)

    def active_count(self, cap: int) -> int:
        """Casos por revisar (contador del menú), contados hasta `cap` (índice status + last_attempt_at)."""
        inner = select(FraudCase.id).where(FraudCase.status.in_(ACTIVE_STATUSES)).limit(cap).subquery()
        return int(self.db.scalar(select(func.count()).select_from(inner)) or 0)

    def attempts_of(self, case_id: int, limit: int) -> list[FraudCaseAttempt]:
        stmt = (
            select(FraudCaseAttempt)
            .where(FraudCaseAttempt.case_id == case_id)
            .order_by(FraudCaseAttempt.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def events_of(self, case_id: int, limit: int) -> list[FraudCaseEvent]:
        stmt = (
            select(FraudCaseEvent)
            .where(FraudCaseEvent.case_id == case_id)
            .order_by(FraudCaseEvent.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def evidence_of(self, case_id: int, limit: int) -> list[FraudEvidence]:
        stmt = select(FraudEvidence).where(FraudEvidence.case_id == case_id).order_by(FraudEvidence.id).limit(limit)
        return list(self.db.scalars(stmt))

    def evidence(self, case_id: int, evidence_id: int) -> FraudEvidence | None:
        stmt = select(FraudEvidence).where(FraudEvidence.id == evidence_id, FraudEvidence.case_id == case_id)
        return self.db.scalar(stmt)

    def employees(self, ids: Sequence[int]) -> dict[int, Employee]:
        """La ficha de los empleados de una página de casos (de varias empresas: alcance de la plataforma), en una
        consulta por su llave primaria. Referencias del historial: también los que están en «Eliminados»."""
        if not ids:
            return {}
        stmt = (
            select(Employee)
            .where(Employee.id.in_(sorted(set(ids))))
            .options(lazyload(Employee.user), lazyload(Employee.company))  # solo el nombre y el número (§3.1.3)
        )
        return {e.id: e for e in self.db.scalars(with_deleted(stmt))}

    # ------------------------------------------------------------------ mantenimiento

    def expired_evidence(self, before: datetime, limit: int) -> list[int]:
        """Evidencia vencida (índice created_at), un lote."""
        stmt = (
            select(FraudEvidence.id)
            .where(FraudEvidence.created_at < before)
            .order_by(FraudEvidence.created_at)
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def delete_evidence(self, ids: Sequence[int]) -> int:
        return affected_rows(self.db, delete(FraudEvidence).where(FraudEvidence.id.in_(ids)))

    def delete_company_evidence(self, company_id: int) -> None:
        """La evidencia de los casos de una empresa que se elimina (fotogramas de rostros: se borran de verdad; sus
        objetos ya se encolaron). Los casos y su historial se conservan con la empresa en «Eliminados»."""
        affected_rows(self.db, delete(FraudEvidence).where(FraudEvidence.company_id == company_id))

    def stale_cases(self, decided_before: datetime, limit: int) -> list[int]:
        """Casos ya decididos hace más de la retención (un lote; índice last_attempt_at + id, lo más viejo primero)."""
        stmt = (
            select(FraudCase.id)
            .where(FraudCase.status.not_in(ACTIVE_STATUSES), FraudCase.last_attempt_at < decided_before)
            .order_by(FraudCase.last_attempt_at)
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def delete_cases(self, ids: Sequence[int]) -> int:
        """Borra casos (sus intentos, historial y evidencia se van en cascada)."""
        return affected_rows(self.db, delete(FraudCase).where(FraudCase.id.in_(ids)))
