from collections.abc import Collection
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models import Company, FaceAttemptMetric, SecurityThreshold


class FaceSecurityRepository:
    """Métricas de los intentos faciales y umbrales autocalibrados (toda la plataforma).

    Cada consulta está acotada (ventana de tiempo + LIMIT) y usa los índices de la tabla: los ataques
    contra una empresa (`company_id, reason, created_at`: solo los intentos con esos motivos) y la de toda
    la plataforma (`created_at, id`).
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def add_metric(self, metric: FaceAttemptMetric) -> None:
        self.db.add(metric)

    def success_values(
        self,
        column: InstrumentedAttribute[float | None],
        since: datetime,
        limit: int,
        *conditions: ColumnElement[bool],
    ) -> list[float]:
        """Los valores medidos en los intentos EXITOSOS más recientes (hasta `limit`)."""
        stmt = (
            select(column)
            .where(
                FaceAttemptMetric.created_at >= since,
                FaceAttemptMetric.success.is_(True),
                column.is_not(None),
                *conditions,
            )
            .order_by(FaceAttemptMetric.created_at.desc(), FaceAttemptMetric.id.desc())
            .limit(limit)
        )
        return [float(v) for v in self.db.scalars(stmt) if v is not None]

    def attacks(self, company_id: int, since: datetime, reasons: Collection[str], cap: int) -> int:
        """Intentos sospechosos contra la empresa desde `since` (cuenta con tope: basta saber si llega)."""
        inner = (
            select(FaceAttemptMetric.id)
            .where(
                FaceAttemptMetric.company_id == company_id,
                FaceAttemptMetric.created_at >= since,
                FaceAttemptMetric.reason.in_(reasons),
            )
            .limit(cap)
            .subquery()
        )
        return int(self.db.scalar(select(func.count()).select_from(inner)) or 0)

    def attacked_companies(
        self, since: datetime, reasons: Collection[str], min_attacks: int, limit: int
    ) -> list[tuple[int, str, int]]:
        """Empresas con al menos `min_attacks` intentos sospechosos desde `since` (las más atacadas)."""
        attacks = func.count(FaceAttemptMetric.id).label("attacks")
        stmt = (
            select(Company.id, Company.name, attacks)
            .join(FaceAttemptMetric, FaceAttemptMetric.company_id == Company.id)
            .where(FaceAttemptMetric.created_at >= since, FaceAttemptMetric.reason.in_(reasons))
            .group_by(Company.id, Company.name)
            .having(attacks >= min_attacks)
            .order_by(attacks.desc(), Company.id)
            .limit(limit)
        )
        return [(int(c), str(n), int(a)) for c, n, a in self.db.execute(stmt).all()]

    def thresholds(self) -> list[SecurityThreshold]:
        return list(self.db.scalars(select(SecurityThreshold).order_by(SecurityThreshold.key)))

    def save_threshold(self, key: str, value: float, samples: int, computed_at: datetime) -> None:
        """Guarda (o reemplaza) un umbral calculado: recalcular dos veces da lo mismo (idempotente)."""
        self.db.merge(SecurityThreshold(key=key, value=value, samples=samples, computed_at=computed_at))
