"""Seguridad facial que se mejora sola, en toda la plataforma.

Decisión del dueño del producto: tenga o no una empresa el aprendizaje de su galería, la PLATAFORMA se
mide y se endurece continuamente. Dos mecanismos, ambos automáticos:

1. **Autocalibración** (`recalibrate`, cada FACE_AUTOCALIBRATION_INTERVAL_HOURS desde el
   mantenimiento): con los números de los intentos EXITOSOS recientes (`ops.face_attempt_metrics`)
   calcula para cada señal el valor que casi todas las personas reales superan con holgura
   (percentil × margen) y lo usa como umbral. **Solo endurece**: nunca baja del mínimo de la
   configuración (el piso seguro) ni sube de su tope (para no dejar fuera a las personas reales);
   sin suficientes mediciones se queda en el mínimo. Señales: giro, mirar arriba/abajo, acercarse,
   respuesta al destello y su cociente rostro/fondo (solo mediciones concluyentes) y probabilidad de
   rostro real (se combina con el nivel de anti-spoofing de cada empresa: gana el más estricto). Del protocolo
   de captura (antifraude 2a): micromovimiento de la ráfaga, paralaje de los giros, cociente de ruido
   rostro/fondo y moiré; este último es un MÁXIMO: se endurece bajándolo (percentil alto de las personas
   reales entre el margen), nunca por debajo de su piso ni por encima de su valor de partida. Los
   intentos que la revisión de un caso confirmó como fraude no cuentan (no "envenenan" lo real).
2. **Refuerzo ante ataques** (`under_attack`): si una empresa acumula intentos sospechosos
   (SECURITY_REASONS) en la ventana, sus retos piden el máximo de movimientos hasta que la ventana
   quede limpia.

Los umbrales viven en la BD (`ops.security_thresholds`, compartidos por todas las instancias) y cada
proceso los guarda unos segundos en caché. Calcular es idempotente: dos instancias que recalculan a la
vez escriben lo mismo.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import ColumnElement
from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.ip_intel import ip_intel
from app.i18n import t
from app.models import FaceAttemptMetric
from app.repositories.face_security_repository import FaceSecurityRepository, MetricColumn, pad_metric_column
from app.schemas.face_security import (
    AttackedCompany,
    CaptureProtocolObservation,
    FaceSecurityOverview,
    FlashObservation,
    IpDatabaseFile,
    IpDatabaseStatus,
    SecurityThresholdRead,
)
from app.services.face_service import SECURITY_REASONS

logger = logging.getLogger(__name__)

#: Empresas reforzadas que se muestran al ADMIN.
ATTACKED_LIMIT = 20


def _no_conditions() -> tuple[ColumnElement[bool], ...]:
    return ()


def _conclusive_flash() -> tuple[ColumnElement[bool], ...]:
    return (FaceAttemptMetric.flash_magnitude >= settings.FACE_FLASH_MIN_MAGNITUDE,)


@dataclass(frozen=True)
class Signal:
    """Una señal que se autocalibra: de qué columna sale, su piso, su tope y cómo se aplica el margen."""

    key: str
    #: Llave de su nombre en el catálogo de mensajes (el ADMIN lo lee en su idioma).
    name: str
    column: MetricColumn
    floor: Callable[[], float]
    cap: Callable[[], float]
    #: Escalas (acercarse): el margen se aplica a lo que crece (1 + (v − 1) × margen), no al valor.
    growth: bool = False
    conditions: Callable[[], tuple[ColumnElement[bool], ...]] = field(default=_no_conditions)
    #: Un MÁXIMO (moiré: lo sospechoso está por encima): parte de su tope y se endurece BAJANDO hacia su piso.
    upper: bool = False

    def clamp(self, value: float) -> float:
        """Nunca por debajo del piso ni por encima del tope (los dos los fija la configuración)."""
        return round(max(self.floor(), min(self.cap(), value)), 5)

    def baseline(self) -> float:
        """El valor de partida (sin datos): el piso de un mínimo, el tope de un máximo (lo menos estricto)."""
        return self.cap() if self.upper else self.floor()

    def tightened(self, value: float) -> bool:
        """La plataforma lo endureció respecto de su valor de partida."""
        return value < self.cap() if self.upper else value > self.floor()

    def candidate(self, values: list[float]) -> float:
        """El percentil de las personas reales con el margen (sin suficientes datos: el valor de partida). Un mínimo
        queda bajo casi todas (percentil bajo × margen); un máximo, sobre casi todas (percentil alto ÷ margen)."""
        if len(values) < settings.FACE_AUTOCALIBRATION_MIN_SAMPLES:
            return self.baseline()
        margin = settings.FACE_AUTOCALIBRATION_MARGIN
        if self.upper:
            return self.clamp(float(np.quantile(values, 1 - settings.FACE_AUTOCALIBRATION_PERCENTILE)) / margin)
        low = float(np.quantile(values, settings.FACE_AUTOCALIBRATION_PERCENTILE))
        return self.clamp(1 + (low - 1) * margin if self.growth else low * margin)


SIGNALS = (
    Signal(
        "LIVENESS_YAW",
        "FACE_SIGNAL_LIVENESS_YAW",
        FaceAttemptMetric.yaw_min,
        lambda: settings.FACE_LIVENESS_MIN_YAW_RATIO,
        lambda: settings.FACE_LIVENESS_MAX_YAW_RATIO,
    ),
    Signal(
        "LIVENESS_PITCH",
        "FACE_SIGNAL_LIVENESS_PITCH",
        FaceAttemptMetric.pitch_min,
        lambda: settings.FACE_LIVENESS_MIN_PITCH_DELTA,
        lambda: settings.FACE_LIVENESS_MAX_PITCH_DELTA,
    ),
    Signal(
        "LIVENESS_CLOSER",
        "FACE_SIGNAL_LIVENESS_CLOSER",
        FaceAttemptMetric.closer_min,
        lambda: settings.FACE_LIVENESS_MIN_CLOSER_SCALE,
        lambda: settings.FACE_LIVENESS_MAX_CLOSER_SCALE,
        growth=True,
    ),
    Signal(
        "FLASH_SCORE",
        "FACE_SIGNAL_FLASH_SCORE",
        FaceAttemptMetric.flash_score,
        lambda: settings.FACE_FLASH_MIN_SCORE,
        lambda: settings.FACE_FLASH_MAX_SCORE,
        conditions=_conclusive_flash,
    ),
    Signal(
        "FLASH_RATIO",
        "FACE_SIGNAL_FLASH_RATIO",
        FaceAttemptMetric.flash_ratio,
        lambda: settings.FACE_FLASH_MIN_FACE_RATIO,
        lambda: settings.FACE_FLASH_MAX_FACE_RATIO,
        # Como las escalas: el margen se aplica a lo que el rostro responde DE MÁS que el fondo (lo que pasa de 1).
        growth=True,
        conditions=_conclusive_flash,
    ),
    Signal(
        "ANTISPOOF_REAL",
        "FACE_SIGNAL_ANTISPOOF_REAL",
        FaceAttemptMetric.frontal_real_min,
        lambda: 0.0,
        lambda: settings.FACE_AUTOCALIBRATION_MAX_REAL,
    ),
    # --- Protocolo de captura (antifraude 2a) ---
    Signal(
        "BURST_MOTION",
        "FACE_SIGNAL_BURST_MOTION",
        FaceAttemptMetric.burst_motion,
        lambda: settings.FACE_BURST_MIN_MOTION,
        lambda: settings.FACE_BURST_MAX_MOTION,
    ),
    Signal(
        "PARALLAX",
        "FACE_SIGNAL_PARALLAX",
        FaceAttemptMetric.parallax,
        lambda: settings.FACE_PARALLAX_MIN,
        lambda: settings.FACE_PARALLAX_MAX,
    ),
    Signal(
        "NOISE_RATIO",
        "FACE_SIGNAL_NOISE_RATIO",
        FaceAttemptMetric.noise_ratio,
        lambda: settings.FACE_NOISE_MIN_RATIO,
        lambda: settings.FACE_NOISE_MAX_RATIO,
    ),
    Signal(
        "MOIRE",
        "FACE_SIGNAL_MOIRE",
        FaceAttemptMetric.moire,
        lambda: settings.FACE_MOIRE_TIGHTEST_DB,
        lambda: settings.FACE_MOIRE_MAX_DB,
        upper=True,
    ),
    # --- PAD de frontera (migración 0090): una por familia de rasgos (`facial_recognition/pad.py`). Un MÁXIMO (lo
    # sospechoso es ALTO): parte de su tope por familia y la autocalibración lo endurece BAJÁNDOLO hacia el piso común
    # (nunca por debajo). Lee su valor del JSON `pad` de los intentos exitosos (`pad_metric_column`). ---
    Signal(
        "PAD_TEXTURE",
        "FACE_SIGNAL_PAD_TEXTURE",
        pad_metric_column("texture"),
        lambda: settings.FACE_PAD_TIGHTEST,
        lambda: settings.FACE_PAD_TEXTURE_MAX,
        upper=True,
    ),
    Signal(
        "PAD_FREQUENCY",
        "FACE_SIGNAL_PAD_FREQUENCY",
        pad_metric_column("frequency"),
        lambda: settings.FACE_PAD_TIGHTEST,
        lambda: settings.FACE_PAD_FREQUENCY_MAX,
        upper=True,
    ),
    Signal(
        "PAD_COLOR",
        "FACE_SIGNAL_PAD_COLOR",
        pad_metric_column("color"),
        lambda: settings.FACE_PAD_TIGHTEST,
        lambda: settings.FACE_PAD_COLOR_MAX,
        upper=True,
    ),
    Signal(
        "PAD_NOISE",
        "FACE_SIGNAL_PAD_NOISE",
        pad_metric_column("noise"),
        lambda: settings.FACE_PAD_TIGHTEST,
        lambda: settings.FACE_PAD_NOISE_MAX,
        upper=True,
    ),
    Signal(
        "PAD_SPECULAR",
        "FACE_SIGNAL_PAD_SPECULAR",
        pad_metric_column("specular"),
        lambda: settings.FACE_PAD_TIGHTEST,
        lambda: settings.FACE_PAD_SPECULAR_MAX,
        upper=True,
    ),
    Signal(
        "PAD_SHARPNESS",
        "FACE_SIGNAL_PAD_SHARPNESS",
        pad_metric_column("sharpness"),
        lambda: settings.FACE_PAD_TIGHTEST,
        lambda: settings.FACE_PAD_SHARPNESS_MAX,
        upper=True,
    ),
    Signal(
        "PAD_CHROMA",
        "FACE_SIGNAL_PAD_CHROMA",
        pad_metric_column("chroma"),
        lambda: settings.FACE_PAD_TIGHTEST,
        lambda: settings.FACE_PAD_CHROMA_MAX,
        upper=True,
    ),
)
SIGNAL_BY_KEY = {s.key: s for s in SIGNALS}


@dataclass(frozen=True)
class SecurityThresholds:
    """Los umbrales vigentes (autocalibrados, o los mínimos de la configuración)."""

    min_yaw_ratio: float
    min_pitch_delta: float
    min_closer_scale: float
    flash_min_score: float
    #: Cociente mínimo rostro/fondo del destello (solo decide con el destello obligatorio).
    flash_min_ratio: float
    #: Piso de la probabilidad de rostro real (se combina con el nivel de cada empresa).
    min_real_probability: float
    #: Protocolo de captura (antifraude 2a): micromovimiento mínimo de la ráfaga, paralaje mínimo de los giros,
    #: cociente mínimo de ruido rostro/fondo y moiré máximo (dB).
    burst_min_motion: float
    min_parallax: float
    min_noise_ratio: float
    max_moire: float
    #: PAD de frontera (migración 0090): el máximo autocalibrado de cada familia de rasgos (`pad.FAMILIES`); por encima,
    #: su señal se dispara (en «Solo medir»: nunca niega sola).
    pad_texture: float
    pad_frequency: float
    pad_color: float
    pad_noise: float
    pad_specular: float
    pad_sharpness: float
    pad_chroma: float


#: El campo de `SecurityThresholds` de cada señal autocalibrada (una sola fuente para leerlos y mostrarlos).
THRESHOLD_FIELDS = {
    "LIVENESS_YAW": "min_yaw_ratio",
    "LIVENESS_PITCH": "min_pitch_delta",
    "LIVENESS_CLOSER": "min_closer_scale",
    "FLASH_SCORE": "flash_min_score",
    "FLASH_RATIO": "flash_min_ratio",
    "ANTISPOOF_REAL": "min_real_probability",
    "BURST_MOTION": "burst_min_motion",
    "PARALLAX": "min_parallax",
    "NOISE_RATIO": "min_noise_ratio",
    "MOIRE": "max_moire",
    "PAD_TEXTURE": "pad_texture",
    "PAD_FREQUENCY": "pad_frequency",
    "PAD_COLOR": "pad_color",
    "PAD_NOISE": "pad_noise",
    "PAD_SPECULAR": "pad_specular",
    "PAD_SHARPNESS": "pad_sharpness",
    "PAD_CHROMA": "pad_chroma",
}


def _thresholds(values: dict[str, float]) -> SecurityThresholds:
    def value(signal: Signal) -> float:
        stored = values.get(signal.key) if settings.FACE_AUTOCALIBRATION_ENABLED else None
        # Lo guardado se vuelve a acotar: si después se cambió el piso o el tope, manda la configuración.
        return signal.baseline() if stored is None else signal.clamp(stored)

    return SecurityThresholds(**{THRESHOLD_FIELDS[signal.key]: value(signal) for signal in SIGNALS})


_cache: tuple[float, SecurityThresholds] | None = None
_lock = threading.Lock()


def clear_threshold_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def thresholds(db: Session) -> SecurityThresholds:
    """Umbrales vigentes (caché por proceso de `FACE_THRESHOLDS_CACHE_SECONDS`; después se vuelven a leer de la
    BD)."""
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache is not None and now - _cache[0] < settings.FACE_THRESHOLDS_CACHE_SECONDS:
            return _cache[1]
    current = _thresholds({row.key: row.value for row in FaceSecurityRepository(db).thresholds()})
    with _lock:
        _cache = (now, current)
    return current


def recalibrate(db: Session, now: datetime) -> int:
    """Recalcula todos los umbrales con los intentos exitosos de la ventana; devuelve cuántos cambiaron."""
    repo = FaceSecurityRepository(db)
    before = {row.key: row.value for row in repo.thresholds()}
    since = now - timedelta(days=settings.FACE_AUTOCALIBRATION_WINDOW_DAYS)
    changed = 0
    for signal in SIGNALS:
        values = repo.success_values(
            signal.column, since, settings.FACE_AUTOCALIBRATION_MAX_SAMPLES, *signal.conditions()
        )
        value = signal.candidate(values)
        if before.get(signal.key) != value:
            changed += 1
            logger.info(
                "Autocalibración: %s = %s (antes %s, %d mediciones)",
                signal.key,
                value,
                before.get(signal.key),
                len(values),
            )
        repo.save_threshold(signal.key, value, len(values), now)
    db.commit()
    clear_threshold_cache()
    return changed


def recalibrate_if_due(db: Session, now: datetime) -> int:
    """Desde el mantenimiento: recalcula si la última vez fue hace más de FACE_AUTOCALIBRATION_INTERVAL_HOURS."""
    if not settings.FACE_AUTOCALIBRATION_ENABLED:
        return 0
    rows = FaceSecurityRepository(db).thresholds()
    interval = timedelta(hours=settings.FACE_AUTOCALIBRATION_INTERVAL_HOURS)
    if len(rows) == len(SIGNALS) and all(now - as_utc(row.computed_at) < interval for row in rows):
        return 0
    return recalibrate(db, now)


def under_attack(db: Session, company_id: int, now: datetime) -> bool:
    """La empresa acumula intentos sospechosos en la ventana: sus retos se refuerzan."""
    since = now - timedelta(minutes=settings.FACE_ESCALATION_WINDOW_MINUTES)
    needed = settings.FACE_ESCALATION_MIN_ATTACKS
    return FaceSecurityRepository(db).attacks(company_id, since, SECURITY_REASONS, needed) >= needed


def _flash_observation(repo: FaceSecurityRepository, since: datetime) -> FlashObservation:
    limit = settings.FACE_AUTOCALIBRATION_MAX_SAMPLES
    scores = repo.success_values(FaceAttemptMetric.flash_score, since, limit, *_conclusive_flash())
    magnitudes = repo.success_values(FaceAttemptMetric.flash_magnitude, since, limit)
    ratios = repo.success_values(FaceAttemptMetric.flash_ratio, since, limit, *_conclusive_flash())
    weak = sum(m < settings.FACE_FLASH_MIN_MAGNITUDE for m in magnitudes)

    def quantile(values: list[float], q: float) -> float | None:
        return round(float(np.quantile(values, q)), 4) if values else None

    return FlashObservation(
        measured=len(magnitudes),
        conclusive=len(scores),
        inconclusive=weak,
        score_median=quantile(scores, 0.5),
        score_p10=quantile(scores, 0.1),
        magnitude_median=quantile(magnitudes, 0.5),
        ratio_median=quantile(ratios, 0.5),
        ratio_p10=quantile(ratios, 0.1),
    )


def _protocol_observation(repo: FaceSecurityRepository, since: datetime) -> CaptureProtocolObservation:
    """El destello dictado y la ráfaga en los intentos exitosos de la ventana (UNA lectura acotada)."""
    rows = repo.protocol_values(since, settings.FACE_AUTOCALIBRATION_MAX_SAMPLES)
    window = settings.FACE_FLASH_PACE_WINDOW_MS
    paces = [row.flash_pace_ms for row in rows if row.flash_pace_ms is not None]
    pulses = [row.pulse_snr for row in rows if row.pulse_snr is not None]

    def quantile(values: list[float], q: float) -> float | None:
        return round(float(np.quantile(values, q)), 2) if values else None

    return CaptureProtocolObservation(
        flash_attempts=sum(row.flash_score is not None or row.flash_pace_ms is not None for row in rows),
        paced=len(paces),
        late=sum(pace > window for pace in paces),
        pace_p50_ms=quantile(paces, 0.5),
        pace_p95_ms=quantile(paces, 0.95),
        window_ms=window,
        liveness_attempts=sum(row.steps is not None and row.steps > 0 for row in rows),
        bursts=sum(row.burst_frames is not None for row in rows),
        pulse_measured=len(pulses),
        pulse_seen=sum(snr >= settings.FACE_PULSE_MIN_SNR for snr in pulses),
        pulse_median_snr=quantile(pulses, 0.5),
    )


def overview(db: Session, now: datetime) -> FaceSecurityOverview:
    """Lo que ve el ADMIN: umbrales vigentes frente a su piso y tope, empresas reforzadas, lo medido del destello
    (para decidir cuándo exigirlo) y del protocolo de captura (destello dictado y ráfaga)."""
    repo = FaceSecurityRepository(db)
    stored = {row.key: row for row in repo.thresholds()}
    current = _thresholds({key: row.value for key, row in stored.items()})
    values = {key: getattr(current, name) for key, name in THRESHOLD_FIELDS.items()}
    window = now - timedelta(minutes=settings.FACE_ESCALATION_WINDOW_MINUTES)
    attacked = repo.attacked_companies(window, SECURITY_REASONS, settings.FACE_ESCALATION_MIN_ATTACKS, ATTACKED_LIMIT)
    return FaceSecurityOverview(
        autocalibration=settings.FACE_AUTOCALIBRATION_ENABLED,
        window_days=settings.FACE_AUTOCALIBRATION_WINDOW_DAYS,
        min_samples=settings.FACE_AUTOCALIBRATION_MIN_SAMPLES,
        interval_hours=settings.FACE_AUTOCALIBRATION_INTERVAL_HOURS,
        thresholds=[
            SecurityThresholdRead(
                key=signal.key,
                name=t(signal.name),
                value=values[signal.key],
                floor=signal.floor(),
                cap=signal.cap(),
                samples=stored[signal.key].samples if signal.key in stored else 0,
                computed_at=stored[signal.key].computed_at if signal.key in stored else None,
                raised=signal.tightened(values[signal.key]),
                upper=signal.upper,
            )
            for signal in SIGNALS
        ],
        escalation_min_attacks=settings.FACE_ESCALATION_MIN_ATTACKS,
        escalation_window_minutes=settings.FACE_ESCALATION_WINDOW_MINUTES,
        reinforced=[AttackedCompany(company_id=c, name=n, attacks=a) for c, n, a in attacked],
        flash=_flash_observation(repo, now - timedelta(days=settings.FACE_AUTOCALIBRATION_WINDOW_DAYS)),
        protocol=_protocol_observation(repo, now - timedelta(days=settings.FACE_AUTOCALIBRATION_WINDOW_DAYS)),
        ip_database=IpDatabaseStatus(
            refresh_enabled=settings.IP_DB_REFRESH_ENABLED,
            refresh_days=settings.IP_DB_REFRESH_DAYS,
            country=_ip_file(ip_intel.country_db.built()),
            asn=_ip_file(ip_intel.asn_db.built()),
        ),
    )


def _ip_file(built: tuple[str, datetime] | None) -> IpDatabaseFile | None:
    return None if built is None else IpDatabaseFile(database_type=built[0], built_at=built[1])
