"""Deriva de las señales del motor facial (antifraude fase 3, I+D §3.5; migración 0081).

Lo que el mantenimiento calcula al cerrar cada ventana (`DRIFT_WINDOW_DAYS`, semanas alineadas al lunes), fuera de
toda petición y una instancia a la vez (`drift_service`):

- `SignalDrift` (datos de la PLATAFORMA, sin `company_id` ni seguridad por fila): una fila por ventana × señal ×
  plataforma (iPhone/iPad, Android, escritorio, otros; la plataforma sale del navegador de cada intento,
  `face_attempt_metrics.platform`). Compara los intentos GENUINOS (aprobados sin caso de fraude confirmado) de la
  ventana con la anterior: mediana, la cola que vigila la señal (el p10 de un mínimo, el p90 de un máximo) y el índice
  de estabilidad de población (PSI). Acotada: ≈ 8 señales × 4 plataformas por ventana y `DRIFT_RETENTION_DAYS` de
  retención (depuración por lotes), así que no se particiona.
- `CompanyFraudWeekly` (tabla de EMPRESA: `company_id NOT NULL` y seguridad por fila; solo la lee el ADMIN): por
  empresa y ventana, los intentos, los casos de fraude abiertos, las revisiones decididas, las aprobadas y las
  aprobadas "sin mirar" (en menos de `DRIFT_QUICK_REVIEW_SECONDS` desde que se abrieron): la señal de fraude interno
  del documento de I+D. El nombre de la empresa se copia al calcular: la lista no une con `companies`.
- `EngineVersion`: la bitácora del motor. Cada cambio de versión de un componente (motor de riesgo, modelos
  faciales, API, aplicación web) se anota al detectarse; una ventana en la que cambió el motor o los modelos no se
  compara con la anterior (`VERSION_CHANGE`): la línea base nunca mezcla versiones incompatibles.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import OPS, TENANCY

#: Estados de una fila de deriva (la app los nombra en sus diccionarios: no es un catálogo).
DRIFT_OK = "OK"
DRIFT_ALERT = "ALERT"
DRIFT_INSUFFICIENT = "INSUFFICIENT"
DRIFT_NO_BASELINE = "NO_BASELINE"
DRIFT_VERSION_CHANGE = "VERSION_CHANGE"
DRIFT_STATUSES = (DRIFT_OK, DRIFT_ALERT, DRIFT_INSUFFICIENT, DRIFT_NO_BASELINE, DRIFT_VERSION_CHANGE)


class SignalDrift(Base):
    """Una señal × plataforma en una ventana, frente a la ventana anterior."""

    __tablename__ = "signal_drift"
    __table_args__ = (
        # La fila de cada señal y plataforma en una ventana (el cálculo reemplaza la ventana completa) y el listado del
        # ADMIN, que siempre filtra por ventana: `WHERE week_start = ? ORDER BY signal, platform` va por este índice.
        UniqueConstraint("week_start", "signal", "platform", name="uq_signal_drift_window"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Primer día de la ventana (lunes).
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    #: Llave de la señal (`face_security.SIGNALS`: LIVENESS_YAW, ANTISPOOF_REAL, MOIRE...).
    signal: Mapped[str] = mapped_column(String(40), nullable=False)
    #: IOS_SAFARI, ANDROID_CHROME, DESKTOP u OTHER (`app/core/devices.py`, `platform_of`).
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    samples: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    median: Mapped[float | None] = mapped_column(Float)
    baseline_median: Mapped[float | None] = mapped_column(Float)
    #: La cola vigilada (p10 de un mínimo, p90 de un máximo), en las dos ventanas.
    tail: Mapped[float | None] = mapped_column(Float)
    baseline_tail: Mapped[float | None] = mapped_column(Float)
    tail_percentile: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Cambio de la cola respecto de la línea base (fracción; negativo = cayó).
    tail_change: Mapped[float | None] = mapped_column(Float)
    psi: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompanyFraudWeekly(Base):
    """Una empresa en una ventana: tasa de casos y revisiones aprobadas sin mirar (fraude interno)."""

    __tablename__ = "company_fraud_weekly"
    __table_args__ = (
        # La fila de la empresa en la ventana (el cálculo reemplaza la ventana) y la FK de la empresa (CASCADE).
        UniqueConstraint("company_id", "week_start", name="uq_company_fraud_weekly_window"),
        # El listado del ADMIN filtra siempre por ventana (una fila por empresa: se ordena en memoria lo de UNA
        # ventana, acotado por el número de empresas) y la depuración recorre las ventanas vencidas.
        Index("ix_company_fraud_weekly_week", "week_start", "id"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    #: Nombre de la empresa al calcular (sin JOIN al listar; una empresa en «Eliminados» conserva el suyo).
    company_name: Mapped[str] = mapped_column(String(150), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    fraud_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Casos por intento (None sin intentos).
    case_rate: Mapped[float | None] = mapped_column(Float)
    #: Revisiones decididas en la ventana, las aprobadas y las aprobadas en menos de DRIFT_QUICK_REVIEW_SECONDS.
    reviews: Mapped[int] = mapped_column(Integer, nullable=False)
    approved: Mapped[int] = mapped_column(Integer, nullable=False)
    quick_approvals: Mapped[int] = mapped_column(Integer, nullable=False)
    quick_rate: Mapped[float | None] = mapped_column(Float)
    #: OK, ALERT (aprueba sin mirar) o INSUFFICIENT (menos de DRIFT_MIN_REVIEWS decisiones).
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EngineVersion(Base):
    """Un cambio de versión de un componente, anotado cuando se detectó (bitácora del motor)."""

    __tablename__ = "engine_versions"
    __table_args__ = (
        # La última versión anotada de un componente (¿cambió?) y los cambios dentro de una ventana.
        Index("ix_engine_versions_component", "component", "noted_at", "id"),
        # La lista reciente del ADMIN y la depuración por antigüedad.
        Index("ix_engine_versions_noted", "noted_at", "id"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: risk_engine, face_models, api o webapp (`engine_log.COMPONENTS`).
    component: Mapped[str] = mapped_column(String(30), nullable=False)
    version: Mapped[str] = mapped_column(String(120), nullable=False)
    noted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
