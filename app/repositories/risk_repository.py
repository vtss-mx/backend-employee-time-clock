"""Consultas del motor de riesgo: huellas perceptuales de las capturas, decisiones de cada intento, la lista de
bloqueo, la línea base de cada señal y el historial de la política (sin reglas de negocio ni `commit`)."""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Row, and_, case, func, select, update
from sqlalchemy.orm import Session

from app.models import AttackSignature, CaptureTrace, FaceAttemptMetric, PolicyChange, RiskAssessment, RiskSignalStat
from app.repositories.aggregates import affected_rows, dialect_insert, paginate


class CaptureTraceRepository:
    """Huellas perceptuales de las capturas frontales de UNA empresa."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def add_all(self, traces: Iterable[CaptureTrace]) -> None:
        self.db.add_all(traces)

    def recent(self, employee_id: int, since: datetime, limit: int) -> Sequence[Row[int, int, bytes, int]]:
        """Las capturas recientes del empleado, la más nueva primero (índice company_id + employee_id + created_at).
        Solo lo necesario para comparar: huellas y el embedding cifrado."""
        stmt = (
            select(
                CaptureTrace.face_phash,
                CaptureTrace.frame_phash,
                CaptureTrace.embedding_encrypted,
                CaptureTrace.dimension,
            )
            .where(
                CaptureTrace.company_id == self.company_id,
                CaptureTrace.employee_id == employee_id,
                CaptureTrace.created_at >= since,
            )
            .order_by(CaptureTrace.created_at.desc(), CaptureTrace.id.desc())
            .limit(limit)
        )
        return self.db.execute(stmt).all()


class RiskAssessmentRepository:
    """Decisiones del motor de riesgo (tabla particionada por mes en `created_at`)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, assessment: RiskAssessment) -> None:
        self.db.add(assessment)

    def window(self, company_id: int, since: datetime, limit: int) -> Sequence[Row[Any, str, str | None, bool]]:
        """Las decisiones recientes de una empresa (simulación), la más nueva primero y con tope (índice
        company_id + created_at + id; la fecha poda las particiones)."""
        stmt = (
            select(RiskAssessment.reasons, RiskAssessment.action, RiskAssessment.fraud_label, RiskAssessment.step_up)
            .where(RiskAssessment.company_id == company_id, RiskAssessment.created_at >= since)
            .order_by(RiskAssessment.created_at.desc(), RiskAssessment.id.desc())
            .limit(limit)
        )
        return self.db.execute(stmt).all()

    def label(self, refs: Sequence[tuple[int, datetime]], label: str | None) -> int:
        """Etiqueta (o limpia) decisiones por su llave (id, fecha): la fecha va en el WHERE y poda las particiones."""
        return _label(self.db, RiskAssessment, refs, label)


class MetricLabelRepository:
    """Etiqueta de revisión de las métricas de los intentos de un caso (la calibración ignora los fraudes)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def label(self, refs: Sequence[tuple[int, datetime]], label: str | None) -> int:
        return _label(self.db, FaceAttemptMetric, refs, label)


def _label(db: Session, model: Any, refs: Sequence[tuple[int, datetime]], label: str | None) -> int:
    """UNA sentencia: las filas (id, created_at) de una tabla particionada; la cota de fechas poda las particiones."""
    if not refs:
        return 0
    ids = [ref_id for ref_id, _ in refs]
    moments = [moment for _, moment in refs]
    stmt = (
        update(model)
        .where(model.id.in_(ids), model.created_at >= min(moments), model.created_at <= max(moments))
        .values(fraud_label=label)
    )
    return affected_rows(db, stmt)


class AttackSignatureRepository:
    """Lista de bloqueo de la plataforma (huellas de ataques confirmados)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def active(self, now: datetime, limit: int) -> Sequence[Row[int, str, int | None]]:
        """Las firmas vigentes y bloqueadas (id, valor, alcance), con tope: cada proceso las guarda en memoria."""
        stmt = (
            select(AttackSignature.id, AttackSignature.value, AttackSignature.company_id)
            .where(AttackSignature.expires_at > now, AttackSignature.allowed.is_(False))
            .order_by(AttackSignature.id)
            .limit(limit)
        )
        return self.db.execute(stmt).all()

    def record_hits(self, ids: Sequence[int], now: datetime) -> None:
        """Cuántas veces coincidió cada firma (UNA sentencia; solo cuando hay coincidencias, que son raras)."""
        affected_rows(
            self.db,
            update(AttackSignature)
            .where(AttackSignature.id.in_(ids))
            .values(hits=AttackSignature.hits + 1, last_hit_at=now),
        )

    def upsert(
        self,
        kind: str,
        values: Sequence[str],
        company_id: int | None,
        case_id: int,
        expires_at: datetime,
        *,
        allowed: bool,
        companies: int = 1,
    ) -> None:
        """Bloquea (o libera, `allowed`) las huellas en UNA sentencia: una ya conocida se renueva con la decisión
        nueva (la última decisión humana manda)."""
        if not values:
            return
        rows = [
            {
                "kind": kind,
                "value": value,
                "company_id": company_id,
                "case_id": case_id,
                "expires_at": expires_at,
                "allowed": allowed,
                "companies": companies,
            }
            for value in sorted(set(values))
        ]
        stmt = dialect_insert(self.db, AttackSignature).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["kind", "value", "company_id"],
            set_={
                "allowed": stmt.excluded.allowed,
                "expires_at": stmt.excluded.expires_at,
                "case_id": stmt.excluded.case_id,
                "companies": stmt.excluded.companies,
            },
        )
        self.db.execute(stmt)

    def companies_blocking(self, kind: str, values: Sequence[str]) -> dict[str, int]:
        """En cuántas empresas está bloqueada cada huella (para volverla de toda la plataforma, decisión D6)."""
        if not values:
            return {}
        stmt = (
            select(AttackSignature.value, func.count())
            .where(
                AttackSignature.kind == kind,
                AttackSignature.value.in_(sorted(set(values))),
                AttackSignature.company_id.is_not(None),
                AttackSignature.allowed.is_(False),
            )
            .group_by(AttackSignature.value)
        )
        return {str(value): int(count) for value, count in self.db.execute(stmt)}

    def release_platform(self, kind: str, values: Sequence[str], now: datetime) -> int:
        """Las firmas de plataforma de esas huellas dejan de bloquear (ya no se confirman en suficientes empresas)."""
        if not values:
            return 0
        stmt = (
            update(AttackSignature)
            .where(
                AttackSignature.kind == kind,
                AttackSignature.value.in_(sorted(set(values))),
                AttackSignature.company_id.is_(None),
            )
            .values(allowed=True, expires_at=now)
        )
        return affected_rows(self.db, stmt)


class RiskSignalStatRepository:
    """Línea base por empresa y señal (fraudes confirmados y falsos positivos de sus casos)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def of_company(self) -> dict[str, tuple[int, int]]:
        stmt = select(RiskSignalStat.signal, RiskSignalStat.confirmed, RiskSignalStat.false_positive).where(
            RiskSignalStat.company_id == self.company_id
        )
        return {str(signal): (int(confirmed), int(false)) for signal, confirmed, false in self.db.execute(stmt)}

    def bump(self, signals: Sequence[str], column: str, delta: int, now: datetime) -> None:
        """Suma (o resta, sin bajar de 0) a una columna de varias señales en UNA sentencia (inserta las que falten)."""
        if not signals:
            return
        start = max(0, delta)
        rows = [
            {
                "company_id": self.company_id,
                "signal": signal,
                "confirmed": start if column == "confirmed" else 0,
                "false_positive": start if column == "false_positive" else 0,
                "updated_at": now,
            }
            for signal in sorted(set(signals))
        ]
        stmt = dialect_insert(self.db, RiskSignalStat).values(rows)
        target = getattr(RiskSignalStat, column)
        stmt = stmt.on_conflict_do_update(
            index_elements=["company_id", "signal"],
            set_={column: case((target + delta < 0, 0), else_=target + delta), "updated_at": now},
        )
        self.db.execute(stmt)


class PolicyChangeRepository:
    """Historial de la política de UNA empresa."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def add(self, change: PolicyChange) -> PolicyChange:
        self.db.add(change)
        self.db.flush()
        return change

    def locked(self, change_id: int) -> PolicyChange | None:
        """El cambio de la empresa, bloqueado hasta el commit (aprobar, rechazar o cancelar sin carreras)."""
        stmt = select(PolicyChange).where(PolicyChange.id == change_id, PolicyChange.company_id == self.company_id)
        return self.db.scalar(stmt.with_for_update())

    def search(self, *, status: str | None, offset: int, limit: int) -> tuple[list[PolicyChange], int]:
        """El historial, lo más reciente primero (índice company_id + created_at + id)."""
        stmt = select(PolicyChange).where(PolicyChange.company_id == self.company_id)
        if status is not None:
            stmt = stmt.where(PolicyChange.status == status)
        order = (PolicyChange.created_at.desc(), PolicyChange.id.desc())
        return paginate(self.db, stmt, order, offset=offset, limit=limit)

    def pending_count(self) -> int:
        stmt = select(func.count()).where(
            and_(PolicyChange.company_id == self.company_id, PolicyChange.status == "PENDING")
        )
        return int(self.db.scalar(stmt) or 0)
