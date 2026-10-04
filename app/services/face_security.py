"""Seguridad facial que se mejora sola, en toda la plataforma.

Decisión del dueño del producto: tenga o no una empresa el aprendizaje de su galería, la PLATAFORMA se
mide y se endurece continuamente. Dos mecanismos, ambos automáticos:

1. **Autocalibración** (`recalibrate`, cada FACE_AUTOCALIBRATION_INTERVAL_HOURS desde el
   mantenimiento): con los números de los intentos EXITOSOS recientes (`ops.face_attempt_metrics`)
   calcula para cada señal el valor que casi todas las personas reales superan con holgura
   (percentil × margen) y lo usa como umbral. **Solo endurece**: nunca baja del mínimo de la
   configuración (el piso seguro) ni sube de su tope (para no dejar fuera a las personas reales);
   sin suficientes mediciones se queda en el mínimo. Señales: giro, mirar arriba/abajo, acercarse,
   respuesta al destello (solo mediciones concluyentes) y probabilidad de rostro real (se combina
   con el nivel de anti-spoofing de cada empresa: gana el más estricto).
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
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.clock import as_utc
from app.core.config import settings
from app.models import FaceAttemptMetric
from app.repositories.face_security_repository import FaceSecurityRepository
from app.schemas.face_security import (
    AttackedCompany,
    FaceSecurityOverview,
    FlashObservation,
    SecurityThresholdRead,
)
from app.services.face_service import SECURITY_REASONS

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
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
    name: str
    column: InstrumentedAttribute[float | None]
    floor: Callable[[], float]
    cap: Callable[[], float]
    #: Escalas (acercarse): el margen se aplica a lo que crece (1 + (v − 1) × margen), no al valor.
    growth: bool = False
    conditions: Callable[[], tuple[ColumnElement[bool], ...]] = field(default=_no_conditions)

    def clamp(self, value: float) -> float:
        """Nunca por debajo del piso (solo endurece) ni por encima del tope."""
        return round(max(self.floor(), min(self.cap(), value)), 5)

    def candidate(self, values: list[float]) -> float:
        """El percentil de las personas reales con el margen (sin suficientes datos: el piso)."""
        if len(values) < settings.FACE_AUTOCALIBRATION_MIN_SAMPLES:
            return self.floor()
        low = float(np.quantile(values, settings.FACE_AUTOCALIBRATION_PERCENTILE))
        margin = settings.FACE_AUTOCALIBRATION_MARGIN
        return self.clamp(1 + (low - 1) * margin if self.growth else low * margin)


SIGNALS = (
    Signal(
        "LIVENESS_YAW",
        "Giro mínimo de la cabeza",
        FaceAttemptMetric.yaw_min,
        lambda: settings.FACE_LIVENESS_MIN_YAW_RATIO,
        lambda: settings.FACE_LIVENESS_MAX_YAW_RATIO,
    ),
    Signal(
        "LIVENESS_PITCH",
        "Movimiento mínimo al mirar arriba o abajo",
        FaceAttemptMetric.pitch_min,
        lambda: settings.FACE_LIVENESS_MIN_PITCH_DELTA,
        lambda: settings.FACE_LIVENESS_MAX_PITCH_DELTA,
    ),
    Signal(
        "LIVENESS_CLOSER",
        "Acercamiento mínimo a la cámara",
        FaceAttemptMetric.closer_min,
        lambda: settings.FACE_LIVENESS_MIN_CLOSER_SCALE,
        lambda: settings.FACE_LIVENESS_MAX_CLOSER_SCALE,
        growth=True,
    ),
    Signal(
        "FLASH_SCORE",
        "Respuesta mínima al destello de colores",
        FaceAttemptMetric.flash_score,
        lambda: settings.FACE_FLASH_MIN_SCORE,
        lambda: settings.FACE_FLASH_MAX_SCORE,
        conditions=_conclusive_flash,
    ),
    Signal(
        "ANTISPOOF_REAL",
        "Probabilidad mínima de rostro real",
        FaceAttemptMetric.frontal_real_min,
        lambda: 0.0,
        lambda: settings.FACE_AUTOCALIBRATION_MAX_REAL,
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
    #: Piso de la probabilidad de rostro real (se combina con el nivel de cada empresa).
    min_real_probability: float


def _thresholds(values: dict[str, float]) -> SecurityThresholds:
    def value(key: str) -> float:
        signal = SIGNAL_BY_KEY[key]
        stored = values.get(key) if settings.FACE_AUTOCALIBRATION_ENABLED else None
        # Lo guardado se vuelve a acotar: si después se cambió el piso o el tope, manda la configuración.
        return signal.floor() if stored is None else signal.clamp(stored)

    return SecurityThresholds(
        min_yaw_ratio=value("LIVENESS_YAW"),
        min_pitch_delta=value("LIVENESS_PITCH"),
        min_closer_scale=value("LIVENESS_CLOSER"),
        flash_min_score=value("FLASH_SCORE"),
        min_real_probability=value("ANTISPOOF_REAL"),
    )


_cache: tuple[float, SecurityThresholds] | None = None
_lock = threading.Lock()


def clear_threshold_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def thresholds(db: Session) -> SecurityThresholds:
    """Umbrales vigentes (caché por proceso de `_CACHE_TTL_SECONDS`; después se vuelven a leer de la BD)."""
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache is not None and now - _cache[0] < _CACHE_TTL_SECONDS:
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
    )


def overview(db: Session, now: datetime) -> FaceSecurityOverview:
    """Lo que ve el ADMIN: umbrales vigentes frente a su piso y tope, empresas reforzadas y lo medido
    del destello (para decidir cuándo exigirlo)."""
    repo = FaceSecurityRepository(db)
    stored = {row.key: row for row in repo.thresholds()}
    current = _thresholds({key: row.value for key, row in stored.items()})
    values = {
        "LIVENESS_YAW": current.min_yaw_ratio,
        "LIVENESS_PITCH": current.min_pitch_delta,
        "LIVENESS_CLOSER": current.min_closer_scale,
        "FLASH_SCORE": current.flash_min_score,
        "ANTISPOOF_REAL": current.min_real_probability,
    }
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
                name=signal.name,
                value=values[signal.key],
                floor=signal.floor(),
                cap=signal.cap(),
                samples=stored[signal.key].samples if signal.key in stored else 0,
                computed_at=stored[signal.key].computed_at if signal.key in stored else None,
                raised=values[signal.key] > signal.floor(),
            )
            for signal in SIGNALS
        ],
        escalation_min_attacks=settings.FACE_ESCALATION_MIN_ATTACKS,
        escalation_window_minutes=settings.FACE_ESCALATION_WINDOW_MINUTES,
        reinforced=[AttackedCompany(company_id=c, name=n, attacks=a) for c, n, a in attacked],
        flash=_flash_observation(repo, now - timedelta(days=settings.FACE_AUTOCALIBRATION_WINDOW_DAYS)),
    )
