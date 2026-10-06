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
    raised: bool = Field(description="La plataforma lo endureció (un mínimo que subió o un máximo que bajó)")
    #: Es un MÁXIMO (antifraude 2a: el moiré): se endurece bajándolo, desde su tope (`cap`) hacia su piso (`floor`).
    upper: bool = False


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
    #: Cociente rostro/fondo de las mediciones concluyentes (≈ 1: el destello tiñe igual rostro y fondo, como una
    #: pantalla o un papel; un rostro real responde más que el fondo).
    ratio_median: float | None = None
    ratio_p10: float | None = None


class IpDatabaseFile(BaseModel):
    """Un archivo de la base local de IP abierto en este proceso: su tipo y cuándo lo construyó DB-IP."""

    database_type: str
    built_at: datetime


class IpDatabaseStatus(BaseModel):
    """La base local de IP (DB-IP Lite, CC BY 4.0; decisión D8): sin ella, las señales de red no se miden."""

    refresh_enabled: bool
    refresh_days: int
    country: IpDatabaseFile | None = None
    asn: IpDatabaseFile | None = None


class CaptureProtocolObservation(BaseModel):
    """Lo medido del protocolo de captura en los intentos exitosos de la ventana (antifraude 2a): para decidir cuándo
    exigir el destello dictado y la ráfaga sin dejar fuera a personas reales."""

    #: Intentos con destello medido y, de ellos, cuántos fueron dictados por el servidor y cuántos a destiempo.
    flash_attempts: int
    paced: int
    late: int
    #: Tiempos de respuesta de un color dictado (ms; la respuesta más lenta de cada intento) y su ventana.
    pace_p50_ms: float | None
    pace_p95_ms: float | None
    window_ms: int
    #: Intentos con prueba de vida (los que debían traer ráfaga) y cuántos la trajeron analizable.
    liveness_attempts: int
    bursts: int
    #: Pulso por video (solo se mide): cuántos se midieron, cuántos se vieron (SNR ≥ el mínimo) y la SNR mediana.
    pulse_measured: int
    pulse_seen: int
    pulse_median_snr: float | None


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
    #: Antifraude 2a: el destello dictado y la ráfaga.
    protocol: CaptureProtocolObservation
    #: Antifraude 1b: la base local de IP de este proceso (país y red de cada intento).
    ip_database: IpDatabaseStatus
