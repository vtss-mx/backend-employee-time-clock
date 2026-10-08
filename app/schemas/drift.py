"""Deriva de las señales del motor facial (antifraude fase 3; pantalla «Deriva de señales» del ADMIN). Las filas las
calcula el mantenimiento por ventana (`DRIFT_WINDOW_DAYS`); aquí solo se leen."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Page

DriftPlatform = Literal["IOS_SAFARI", "ANDROID_CHROME", "DESKTOP", "OTHER"]
DriftStatus = Literal["OK", "ALERT", "INSUFFICIENT", "NO_BASELINE", "VERSION_CHANGE"]


class DriftRow(BaseModel):
    """Una señal × plataforma en una ventana, frente a la ventana anterior."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    week_start: date
    signal: str
    #: El nombre de la señal en el idioma de la petición (catálogo de mensajes, `FACE_SIGNAL_*`).
    signal_name: str = ""
    platform: str
    samples: int
    baseline_samples: int
    median: float | None = None
    baseline_median: float | None = None
    #: La cola vigilada: el p10 de un mínimo o el p90 de un máximo (`tail_percentile`).
    tail: float | None = None
    baseline_tail: float | None = None
    tail_percentile: int
    #: Cambio de la cola respecto de la línea base (fracción; negativo = cayó).
    tail_change: float | None = None
    #: Índice de estabilidad de población entre las dos ventanas.
    psi: float | None = None
    status: str
    #: Un MÁXIMO (el moiré): lo sospechoso está por encima y la cola vigilada es el p90.
    upper: bool = False
    computed_at: datetime


class DriftList(Page[DriftRow]):
    #: La ventana que se listó (la pedida o la más reciente); None sin ventanas calculadas.
    week_start: date | None = None


class CompanyDriftRow(BaseModel):
    """Una empresa en una ventana: tasa de casos y revisiones aprobadas sin mirar."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    week_start: date
    company_id: int
    company_name: str
    attempts: int
    fraud_cases: int
    case_rate: float | None = None
    reviews: int
    approved: int
    quick_approvals: int
    quick_rate: float | None = None
    status: str
    computed_at: datetime


class CompanyDriftList(Page[CompanyDriftRow]):
    week_start: date | None = None


class EngineVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    component: str
    version: str
    noted_at: datetime


class DriftSummary(BaseModel):
    """La configuración vigente, las ventanas calculadas y los indicadores de la más reciente."""

    window_days: int
    psi_alert: float
    tail_drop_alert: float
    min_samples: int
    quick_review_seconds: int
    quick_approval_ratio: float
    weeks: list[date] = Field(description="Ventanas calculadas (fecha de inicio), la más reciente primero")
    latest_week: date | None = None
    alerts: int
    insufficient: int
    companies_alerted: int
    platforms: list[str]
    versions: list[EngineVersionRead]
    computed_at: datetime | None = None
