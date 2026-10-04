"""Mediciones de un intento facial mientras se analiza (solo números).

Las piezas de `identity_core` anotan aquí lo que miden (probabilidad de rostro real, cuánto se movió la
persona en cada paso, respuesta al destello, tiempos) y `IdentityLog.record` lo guarda junto con el
intento en la bitácora (misma transacción) como una fila de `ops.face_attempt_metrics`. Con esas
filas la plataforma se ajusta sola (`face_security`).

Vive en una variable de contexto de la petición (`ContextVar`): cada petición tiene la suya (FastAPI
copia el contexto a su hilo), así ninguna pieza tiene que pasar el recolector de mano en mano y nada
queda compartido entre peticiones ni entre instancias. `take_challenge`, el primer candado de toda
captura facial, empieza uno nuevo; un flujo sin rostro (solo QR) no tiene recolector y no guarda nada.
"""

import math
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime

from app.facial_recognition import FaceAnalysis, LivenessAction
from app.facial_recognition.photometry import FlashResponse
from app.models import FaceAttemptMetric

#: Familia de cada movimiento en las métricas (giro, mirar arriba/abajo, acercarse).
STEP_FAMILY = {
    LivenessAction.TURN_LEFT: "yaw",
    LivenessAction.TURN_RIGHT: "yaw",
    LivenessAction.LOOK_UP: "pitch",
    LivenessAction.LOOK_DOWN: "pitch",
    LivenessAction.MOVE_CLOSER: "closer",
}


def _finite(values: Sequence[float | None]) -> list[float]:
    return [float(v) for v in values if v is not None and math.isfinite(v)]


def _min(values: Sequence[float | None]) -> float | None:
    clean = _finite(values)
    return round(min(clean), 5) if clean else None


def _mean(values: Sequence[float | None]) -> float | None:
    clean = _finite(values)
    return round(sum(clean) / len(clean), 5) if clean else None


@dataclass
class AttemptSignals:
    """Lo medido en un intento (se llena por partes: algunas no llegan si el intento se detiene antes)."""

    steps: int | None = None
    flash_mode: str | None = None
    response_seconds: float | None = None
    frontal: list[FaceAnalysis] = field(default_factory=list)
    step_real: list[float | None] = field(default_factory=list)
    moves: dict[str, list[float | None]] = field(default_factory=dict)
    flash: FlashResponse | None = None

    def challenged(self, steps: int, issued_at: datetime, now: datetime) -> None:
        """El reto que se respondió: cuántos movimientos pedía y cuánto tardó la respuesta."""
        self.steps = steps
        self.response_seconds = (now - issued_at).total_seconds()

    def add_step(self, action: LivenessAction, analysis: FaceAnalysis) -> None:
        self.step_real.append(analysis.real_probability)
        self.moves.setdefault(STEP_FAMILY[action], []).append(analysis.step_value)

    def metric(self, company_id: int, *, success: bool, reason: str | None) -> FaceAttemptMetric:
        """La fila de métricas del intento (números redondeados; sin imágenes ni plantillas)."""
        flash = self.flash
        return FaceAttemptMetric(
            company_id=company_id,
            success=success,
            reason=reason,
            steps=self.steps,
            flash_mode=self.flash_mode,
            response_seconds=None if self.response_seconds is None else round(self.response_seconds, 2),
            frontal_real_min=_min([f.real_probability for f in self.frontal]),
            frontal_real_mean=_mean([f.real_probability for f in self.frontal]),
            step_real_min=_min(self.step_real),
            yaw_min=_min(self.moves.get("yaw", [])),
            pitch_min=_min(self.moves.get("pitch", [])),
            closer_min=_min(self.moves.get("closer", [])),
            flash_score=flash.score if flash and flash.pairs else None,
            flash_magnitude=flash.magnitude if flash and flash.pairs else None,
            flash_background=flash.background_magnitude if flash else None,
            quality_mean=_mean([f.quality_score for f in self.frontal]),
            brightness_mean=_mean([f.brightness for f in self.frontal]),
        )


_current: ContextVar[AttemptSignals | None] = ContextVar("face_attempt_signals", default=None)


def begin(flash_mode: str | None = None) -> AttemptSignals:
    """Empieza las mediciones del intento de esta petición (reemplaza las de un intento anterior)."""
    signals = AttemptSignals(flash_mode=flash_mode)
    _current.set(signals)
    return signals


def current() -> AttemptSignals:
    """Las mediciones del intento en curso; fuera de un intento (sin `begin`), unas sueltas que nadie
    guarda: quien mide no tiene que preguntar si hay intento."""
    return _current.get() or AttemptSignals()


def finish() -> AttemptSignals | None:
    """Las mediciones del intento (y las olvida: cada intento se guarda una sola vez)."""
    signals = _current.get()
    _current.set(None)
    return signals
