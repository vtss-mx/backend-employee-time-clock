from datetime import datetime

from pydantic import BaseModel, Field


class SecurityThresholdRead(BaseModel):
    """Un umbral de la plataforma: el vigente, su piso (configuración) y su tope."""

    key: str
    name: str
    value: float
    floor: float = Field(description="Mínimo de la configuración: la autocalibración nunca baja de aquí")
    cap: float = Field(description="Tope: la autocalibración nunca sube de aquí (para no bloquear a personas reales)")
    samples: int = Field(description="Intentos exitosos medidos en el último cálculo")
    computed_at: datetime | None = None
    raised: bool = Field(description="La plataforma lo endureció por encima del mínimo")


class AttackedCompany(BaseModel):
    company_id: int
    name: str
    attacks: int


class FlashObservation(BaseModel):
    """Lo medido del destello en los intentos exitosos de la ventana (para decidir cuándo exigirlo)."""

    measured: int
    conclusive: int
    inconclusive: int = Field(description="Con tanta luz ambiente que el destello casi no se notó")
    score_median: float | None = None
    score_p10: float | None = None
    magnitude_median: float | None = None


class FaceSecurityOverview(BaseModel):
    """Seguridad facial automática de la plataforma (solo el ADMIN)."""

    autocalibration: bool
    window_days: int
    min_samples: int
    interval_hours: float
    thresholds: list[SecurityThresholdRead]
    escalation_min_attacks: int
    escalation_window_minutes: int
    reinforced: list[AttackedCompany]
    flash: FlashObservation
