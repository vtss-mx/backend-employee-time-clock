"""Consultas del monitoreo de deriva (antifraude fase 3): los intentos genuinos por señal y plataforma, los casos y las
revisiones por empresa, las filas calculadas por ventana y la bitácora de versiones del motor. Sin reglas de negocio ni
`commit`; todas acotadas (ventana de tiempo + LIMIT o un GROUP BY en la base)."""

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import Row, delete, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.soft_delete import with_deleted
from app.models import (
    AttendanceReviewStatus,
    Company,
    CompanyFraudWeekly,
    EngineVersion,
    FaceAttemptMetric,
    FraudCase,
    SignalDrift,
    WorkSession,
)
from app.models.drift import DRIFT_ALERT, DRIFT_INSUFFICIENT
from app.repositories.aggregates import affected_rows, insert_many, paginate
from app.repositories.face_security_repository import FRAUD_LABEL
from app.repositories.search import contains_text, search_term

#: Las revisiones que la empresa ya decidió (las pendientes no dicen nada de cómo revisa).
REVIEW_DECIDED = (AttendanceReviewStatus.CONFIRMED.value, AttendanceReviewStatus.REJECTED.value)


class DriftRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------ lo que se mide

    def platform_samples(
        self,
        columns: Sequence[InstrumentedAttribute[float | None]],
        platform: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[tuple[Any, ...]]:
        """Los valores de TODAS las señales en los intentos GENUINOS (exitosos y sin fraude confirmado) de una
        plataforma en la ventana, los más recientes primero y con tope (índice `(created_at, id)`, como la calibración;
        la fecha poda las particiones). UNA lectura por plataforma y ventana —no una por señal—: con ≈30 señales y 4
        plataformas son 8 recorridos del rango de la semana por cálculo, no 240 (con volumen, ≈9 ms cada uno)."""
        stmt = (
            select(*columns)
            .where(
                FaceAttemptMetric.created_at >= start,
                FaceAttemptMetric.created_at < end,
                FaceAttemptMetric.success.is_(True),
                FaceAttemptMetric.platform == platform,
                or_(FaceAttemptMetric.fraud_label.is_(None), FaceAttemptMetric.fraud_label != FRAUD_LABEL),
            )
            .order_by(FaceAttemptMetric.created_at.desc(), FaceAttemptMetric.id.desc())
            .limit(limit)
        )
        return [tuple(row) for row in self.db.execute(stmt).all()]

    def attempts_by_company(self, start: datetime, end: datetime) -> dict[int, int]:
        """Intentos faciales de cada empresa en la ventana: UN `GROUP BY` en la base (índice `(created_at, id)`)."""
        stmt = (
            select(FaceAttemptMetric.company_id, func.count())
            .where(FaceAttemptMetric.created_at >= start, FaceAttemptMetric.created_at < end)
            .group_by(FaceAttemptMetric.company_id)
        )
        return {int(company): int(count) for company, count in self.db.execute(stmt).all()}

    def cases_by_company(self, start: datetime, end: datetime) -> dict[int, int]:
        """Casos de fraude con actividad en la ventana, por empresa: se cuentan por su último intento (índice
        `ix_fraud_cases_recent (last_attempt_at, id)`; por `created_at` no hay índice y sería un recorrido completo),
        así un caso que sigue recibiendo intentos cuenta en la semana en que los recibe."""
        stmt = (
            select(FraudCase.company_id, func.count())
            .where(FraudCase.last_attempt_at >= start, FraudCase.last_attempt_at < end)
            .group_by(FraudCase.company_id)
        )
        return {int(company): int(count) for company, count in self.db.execute(stmt).all()}

    def reviews(
        self, start: datetime, end: datetime, limit: int
    ) -> Sequence[Row[int, str | None, datetime, datetime | None]]:
        """Las revisiones DECIDIDAS en la ventana (índice parcial `ix_work_sessions_reviewed`): empresa, decisión,
        cuándo se abrió (la entrada que la dejó en revisión) y cuándo se decidió. Acotadas con tope: solo se revisan los
        registros que el motor dejó en revisión (pocos); la resta de tiempos se hace en Python porque la aritmética de
        fechas no es la misma en PostgreSQL y SQLite."""
        stmt = (
            select(WorkSession.company_id, WorkSession.review_status, WorkSession.check_in_at, WorkSession.reviewed_at)
            .where(
                WorkSession.reviewed_at >= start,
                WorkSession.reviewed_at < end,
                WorkSession.review_status.in_(REVIEW_DECIDED),
            )
            .order_by(WorkSession.reviewed_at, WorkSession.id)
            .limit(limit)
        )
        return self.db.execute(stmt).all()

    def company_names(self, company_ids: Iterable[int]) -> dict[int, str]:
        """Nombre de cada empresa con datos en la ventana (una consulta; también las que están en «Eliminados»: el
        historial las sigue nombrando)."""
        ids = set(company_ids)
        if not ids:
            return {}
        rows = self.db.execute(with_deleted(select(Company.id, Company.name).where(Company.id.in_(ids))))
        return {int(i): str(n) for i, n in rows}

    # ------------------------------------------------------------------ lo calculado

    def replace_signals(self, week_start: date, rows: list[SignalDrift]) -> int:
        """Reemplaza la ventana completa (repetir el cálculo es idempotente): un DELETE y una inserción."""
        affected_rows(self.db, delete(SignalDrift).where(SignalDrift.week_start == week_start))
        return len(insert_many(self.db, rows))

    def replace_companies(self, week_start: date, rows: list[CompanyFraudWeekly]) -> int:
        affected_rows(self.db, delete(CompanyFraudWeekly).where(CompanyFraudWeekly.week_start == week_start))
        return len(insert_many(self.db, rows))

    def weeks(self, limit: int) -> list[date]:
        """Las ventanas calculadas, la más reciente primero (acotadas: las que caben en la pantalla)."""
        stmt = select(SignalDrift.week_start).distinct().order_by(SignalDrift.week_start.desc()).limit(limit)
        return list(self.db.scalars(stmt))

    def has_week(self, week_start: date) -> bool:
        return self.db.scalar(select(SignalDrift.id).where(SignalDrift.week_start == week_start).limit(1)) is not None

    def signals_page(
        self, week_start: date, *, platform: str | None, status: str | None, offset: int, limit: int
    ) -> tuple[list[SignalDrift], int]:
        """Las filas de una ventana (por el único `(week_start, signal, platform)`, en su orden), con sus filtros."""
        stmt = select(SignalDrift).where(SignalDrift.week_start == week_start)
        if platform:
            stmt = stmt.where(SignalDrift.platform == platform)
        if status:
            stmt = stmt.where(SignalDrift.status == status)
        return paginate(self.db, stmt, (SignalDrift.signal, SignalDrift.platform), offset=offset, limit=limit)

    def companies_page(
        self, week_start: date, *, search: str | None, offset: int, limit: int
    ) -> tuple[list[CompanyFraudWeekly], int]:
        """Las empresas de una ventana, las que más aprueban sin mirar primero (una fila por empresa: el orden en
        memoria es de lo de UNA ventana, acotado por el número de empresas); búsqueda literal por nombre."""
        stmt = select(CompanyFraudWeekly).where(CompanyFraudWeekly.week_start == week_start)
        if search:
            stmt = stmt.where(contains_text(func.lower(CompanyFraudWeekly.company_name), search_term(search)))
        order = (
            CompanyFraudWeekly.quick_rate.desc().nulls_last(),
            CompanyFraudWeekly.case_rate.desc().nulls_last(),
            CompanyFraudWeekly.id,
        )
        return paginate(self.db, stmt, order, offset=offset, limit=limit)

    def counts(self, week_start: date) -> tuple[int, int, int]:
        """De una ventana: señales con alerta, sin datos suficientes y empresas con alerta (conteos en la base)."""
        signals = self.db.execute(
            select(SignalDrift.status, func.count())
            .where(SignalDrift.week_start == week_start, SignalDrift.status.in_((DRIFT_ALERT, DRIFT_INSUFFICIENT)))
            .group_by(SignalDrift.status)
        ).all()
        by_status = {str(status): int(count) for status, count in signals}
        companies = self.db.scalar(
            select(func.count())
            .select_from(CompanyFraudWeekly)
            .where(CompanyFraudWeekly.week_start == week_start, CompanyFraudWeekly.status == DRIFT_ALERT)
        )
        return by_status.get(DRIFT_ALERT, 0), by_status.get(DRIFT_INSUFFICIENT, 0), int(companies or 0)

    def computed_at(self, week_start: date) -> datetime | None:
        return self.db.scalar(select(func.max(SignalDrift.computed_at)).where(SignalDrift.week_start == week_start))

    # ------------------------------------------------------------------ bitácora del motor

    def latest_version(self, component: str) -> str | None:
        """La última versión anotada de un componente (índice `(component, noted_at, id)`, leído hacia atrás)."""
        stmt = (
            select(EngineVersion.version)
            .where(EngineVersion.component == component)
            .order_by(EngineVersion.noted_at.desc(), EngineVersion.id.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def add_version(self, component: str, version: str, noted_at: datetime) -> None:
        self.db.add(EngineVersion(component=component, version=version, noted_at=noted_at))

    def versions(self, limit: int) -> list[EngineVersion]:
        """Los cambios más recientes (lista del ADMIN; índice `(noted_at, id)`)."""
        stmt = select(EngineVersion).order_by(EngineVersion.noted_at.desc(), EngineVersion.id.desc()).limit(limit)
        return list(self.db.scalars(stmt))

    def changed_within(self, components: Sequence[str], start: datetime, end: datetime) -> bool:
        """¿Alguno de esos componentes cambió de versión dentro del rango? (una línea base no comparable)."""
        stmt = (
            select(EngineVersion.id)
            .where(
                EngineVersion.component.in_(components),
                EngineVersion.noted_at >= start,
                EngineVersion.noted_at < end,
            )
            .limit(1)
        )
        return self.db.scalar(stmt) is not None
