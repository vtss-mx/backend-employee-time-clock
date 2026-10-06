"""Protocolo de captura de frontera (antifraude fase 2a; docs/rd/antifraude-identidad.md §2.2-§2.3, decisión D11).

Junta en un lugar lo que el servidor pide, valida y deriva del protocolo (el análisis de imágenes vive en
`facial_recognition/burst.py` y en el pipeline; el destello dictado, en `flash_pacing`):

- `burst_spec`: la ráfaga que se pide con el reto (todo sale de la configuración: lado, recortes, cuadros por segundo,
  calidad, margen y tope en bytes).
- `burst_layout`: la descripción de la hoja validada ESTRICTA contra lo pedido (versión, lado, cuántos recortes de cada
  tramo, tramos en orden, tiempos crecientes con separación mínima y duración acotada). Lo que no cumple no es un error:
  es la señal BURST_MISSING.
- `measure_burst`: analiza la hoja con un worker facial (la misma cola acotada), sin transacción abierta, y compara sus
  recortes con las frontales (la misma persona). Una falla del motor aquí solo deja la ráfaga sin medir (se registra):
  nunca cambia la respuesta a la persona. Con la hoja de 36 recortes de 160 px (decisión del dueño, 2026-10-06) también
  representa los mejores recortes quietos (`FACE_CONSENSUS_FRAMES`) para el consenso de identidad de `identity_core`.
- `BurstMeasure`: la misma medición EN PARALELO (decisión del dueño, 2026-10-06: la ráfaga sumaba ≈115-130 ms al
  intento, que corría en un solo núcleo). Empieza antes del destello y los movimientos en otro worker LIBRE
  (`run_on_spare`: nunca espera ni se adelanta a la fila) y su resultado se recoge al final, con tope
  `FACE_BURST_WAIT_SECONDS`. Sin un worker libre (bajo carga, o con un solo worker por proceso) se mide con el de la
  petición, como antes: el paralelo nunca agrega espera ni consume más de lo que hay libre.
- `parallax_of`: el paralaje de cada giro o cabeceo contra la última frontal (los puntos que el pipeline ya tiene).
- `protocol_hits`: las señales del motor de riesgo (reglas puras). Todas nacen en «Solo medir» y como mucho piden más
  (`risk_rules.ASK_ONLY_SIGNALS`); el pulso, además, nunca decide (`MEASURE_ONLY_SIGNALS`).

Privacidad (regla 13): la hoja se analiza en memoria y se descarta; solo quedan números en `ops.face_attempt_metrics`.
La única excepción es la evidencia de un caso de fraude (decisión D1), por su camino de siempre (`STORED_IMAGES`).
"""

import logging
from collections.abc import Sequence
from concurrent.futures import Future
from itertools import pairwise

import cv2
import numpy as np
from pydantic import ValidationError

from app.core.config import settings
from app.facial_recognition import FaceAnalysis, FacePipeline, LivenessAction, run_on_spare
from app.facial_recognition.burst import HOLD, BurstLayout, BurstMalformed, parallax
from app.facial_recognition.matcher import cosine_similarity
from app.facial_recognition.pipeline import BurstRules
from app.models import RiskSignal
from app.schemas.capture import BurstMeta
from app.schemas.verification import BurstSpec
from app.services.face_security import SecurityThresholds
from app.services.face_signals import AttemptSignals, BurstOutcome, PaceOutcome, PaceVerdict
from app.services.liveness_service import LivenessResponse
from app.services.risk_rules import Hit

logger = logging.getLogger(__name__)

#: Movimientos que dejan ver la perspectiva de la nariz (acercarse casi no la cambia: no se mide).
PARALLAX_ACTIONS = frozenset(
    {LivenessAction.TURN_LEFT, LivenessAction.TURN_RIGHT, LivenessAction.LOOK_UP, LivenessAction.LOOK_DOWN}
)
#: La señal de cada veredicto del destello dictado (uno dictado a tiempo no dice nada).
PACE_SIGNALS = {
    PaceVerdict.UNPACED: RiskSignal.FLASH_UNPACED,
    PaceVerdict.TIMING: RiskSignal.FLASH_PACE_TIMING,
    PaceVerdict.MISMATCH: RiskSignal.FLASH_PACE_MISMATCH,
}


def burst_spec() -> BurstSpec:
    """Lo que el reto pide de la ráfaga (de la configuración: una sola fuente para la app y el servidor)."""
    return BurstSpec(
        tile=settings.FACE_BURST_TILE_PX,
        hold=settings.FACE_BURST_HOLD_FRAMES,
        move=settings.FACE_BURST_MOVE_FRAMES,
        fps=settings.FACE_BURST_FPS,
        quality=settings.FACE_BURST_JPEG_QUALITY,
        margin=settings.FACE_BURST_MARGIN,
        max_bytes=max_burst_bytes(),
        min_frames=settings.FACE_BURST_MIN_FRAMES,
    )


def max_burst_bytes() -> int:
    return int(settings.FACE_BURST_MAX_MB * 1024 * 1024)


def max_meta_chars() -> int:
    """Lo más largo que puede ser la descripción de la hoja con lo que se pide (unos 12 caracteres por recorte)."""
    return 64 + 12 * (settings.FACE_BURST_HOLD_FRAMES + settings.FACE_BURST_MOVE_FRAMES)


def burst_rules() -> BurstRules:
    return BurstRules(
        identical=settings.FACE_BURST_IDENTICAL,
        max_jump=settings.FACE_BURST_MAX_JUMP,
        pulse_seconds=settings.FACE_PULSE_MIN_SECONDS,
        pulse_rate=settings.FACE_PULSE_RATE_HZ,
        pulse_low_hz=settings.FACE_PULSE_MIN_HZ,
        pulse_high_hz=settings.FACE_PULSE_MAX_HZ,
        identity_frames=settings.FACE_CONSENSUS_FRAMES,
    )


def _spans_ok(times: Sequence[int]) -> bool:
    """Tiempos crecientes con la separación mínima y un tramo que no dura más de lo permitido."""
    gaps = [after - before for before, after in pairwise(times)]
    return all(gap >= settings.FACE_BURST_MIN_INTERVAL_MS for gap in gaps) and (
        not times or times[-1] - times[0] <= settings.FACE_BURST_MAX_SPAN_MS
    )


def burst_layout(raw: str | None) -> BurstLayout | None:
    """La descripción de la hoja validada contra lo que pidió el servidor; None si no cumple (señal, nunca un 422)."""
    if not raw or len(raw) > max_meta_chars():
        return None
    try:
        meta = BurstMeta.model_validate_json(raw)
    except ValidationError:
        return None
    count, holds = len(meta.t), meta.s.count(HOLD)
    moves = count - holds
    if (
        len(meta.s) != count
        or meta.tile != settings.FACE_BURST_TILE_PX
        or count < settings.FACE_BURST_MIN_FRAMES
        or holds > settings.FACE_BURST_HOLD_FRAMES
        or moves > settings.FACE_BURST_MOVE_FRAMES
        or meta.cols > count
        or meta.t[0] < 0
        or not (_spans_ok(meta.t[:holds]) and _spans_ok(meta.t[holds:]))
        or (holds and moves and meta.t[holds] <= meta.t[holds - 1])
    ):
        return None
    return BurstLayout(tile=meta.tile, cols=meta.cols, times=tuple(meta.t), segments=tuple(meta.s))


def _anchor(anchors: Sequence[np.ndarray], frontal: Sequence[FaceAnalysis]) -> float | None:
    """El parecido MENOR de los recortes ancla con su frontal más parecida: la ráfaga debe ser la misma persona."""
    if not anchors or not frontal:
        return None
    return round(min(max(cosine_similarity(a, f.embedding) for f in frontal) for a in anchors), 4)


def measure_burst(
    pipeline: FacePipeline, response: LivenessResponse, frontal: Sequence[FaceAnalysis]
) -> BurstOutcome | None:
    """La ráfaga del intento (que la pidió): sin hoja, BURST_MISSING; mal formada (más grande de lo permitido, mal
    descrita, ilegible o con otras medidas), la misma señal con valor 1; si no, lo analizado. None: el motor falló al
    medirla (no es culpa de quien la mandó: sin señal, y la falla queda registrada)."""
    if response.burst_oversize:
        return BurstOutcome(malformed=True)
    if response.burst is None:
        return BurstOutcome()
    layout = burst_layout(response.burst_meta)
    if layout is None:
        return BurstOutcome(malformed=True)
    try:
        analysis = pipeline.analyze_burst(response.burst, layout, burst_rules())
    except BurstMalformed, cv2.error, ValueError:
        return BurstOutcome(malformed=True)
    except Exception:
        # Una falla del motor al medir la ráfaga no tumba el intento: queda sin medir y registrada para el ADMIN.
        logger.exception("El motor falló al analizar la ráfaga: el intento sigue sin medirla")
        return None
    return BurstOutcome(analysis=analysis, anchor=_anchor(analysis.anchors, frontal))


class BurstMeasure:
    """La ráfaga de un intento medida en paralelo a su destello y sus movimientos (ver el módulo).

    Al crearla, si llegó una hoja que analizar, la manda a otro worker libre; `result()` la recoge (o, sin worker
    libre, la mide en ese momento con el de la petición: lo mismo que antes, en el mismo orden). Si el intento termina
    antes (un movimiento que no se hizo, un engaño), nadie la recoge: el worker de repuesto se devuelve solo al
    terminar. Las frontales ya están analizadas al crearla, así que el parecido con ellas se calcula allá también."""

    def __init__(self, pipeline: FacePipeline, response: LivenessResponse, frontal: Sequence[FaceAnalysis]) -> None:
        self._pipeline, self._response, self._frontal = pipeline, response, tuple(frontal)
        sheet = response.burst is not None and not response.burst_oversize
        # Solo una hoja que analizar vale otro núcleo: lo que no llegó o llegó de más se resuelve en microsegundos.
        self._future: Future[BurstOutcome | None] | None = (
            run_on_spare(lambda spare: measure_burst(spare, response, self._frontal)) if sheet else None
        )

    def result(self) -> BurstOutcome | None:
        """Lo medido (None: el motor falló o no terminó a tiempo; queda registrado y el intento sigue sin medirla)."""
        if self._future is None:
            return measure_burst(self._pipeline, self._response, self._frontal)
        try:
            return self._future.result(timeout=settings.FACE_BURST_WAIT_SECONDS)
        except Exception:
            # Un tiempo agotado (o una falla inesperada del hilo) nunca tumba el intento: queda sin medir y registrada.
            logger.exception("La ráfaga medida en paralelo no terminó: el intento sigue sin medirla")
            return None


def parallax_of(
    frontal: Sequence[FaceAnalysis], turns: Sequence[FaceAnalysis], actions: Sequence[LivenessAction]
) -> float | None:
    """El paralaje MENOR de los giros y cabeceos del reto contra la última frontal (None si no hubo uno medible)."""
    reference = frontal[-1].landmarks if frontal else None
    if reference is None:
        return None
    values = [
        parallax(reference, turn.landmarks)
        for action, turn in zip(actions, turns, strict=False)
        if action in PARALLAX_ACTIONS and turn.landmarks is not None
    ]
    return min(values) if values else None


def _burst_hits(burst: BurstOutcome, limits: SecurityThresholds) -> list[Hit]:
    analysis = burst.analysis
    if analysis is None:
        return [Hit(RiskSignal.BURST_MISSING, 1.0 if burst.malformed else 0.0)]
    hits: list[Hit] = []
    tolerated = int(analysis.frames * settings.FACE_BURST_MAX_FACELESS)
    mismatch = burst.anchor is not None and burst.anchor < settings.FACE_LIVENESS_CONSISTENCY_THRESHOLD
    breaks = analysis.jumps + (analysis.faceless if analysis.faceless > tolerated else 0) + int(mismatch)
    if breaks:
        hits.append(Hit(RiskSignal.BURST_DISCONTINUOUS, float(breaks)))
    if analysis.motion is not None and analysis.motion < limits.burst_min_motion:
        hits.append(Hit(RiskSignal.BURST_FROZEN, analysis.motion, limits.burst_min_motion))
    if analysis.repeats:
        hits.append(Hit(RiskSignal.BURST_LOOP, float(analysis.repeats)))
    pulse = analysis.pulse
    if pulse is not None and pulse.snr_db < settings.FACE_PULSE_MIN_SNR:
        hits.append(Hit(RiskSignal.PULSE_ABSENT, pulse.snr_db, settings.FACE_PULSE_MIN_SNR))
    return hits


def _physical_hits(signals: AttemptSignals, limits: SecurityThresholds) -> list[Hit]:
    """Las señales físicas de las frontales (el peor caso) y el paralaje de los giros."""
    hits: list[Hit] = []
    screens = [f.moire for f in signals.frontal if f.moire is not None]
    if screens and max(screens) > limits.max_moire:
        hits.append(Hit(RiskSignal.MOIRE_HIGH, round(max(screens), 3), limits.max_moire))
    noises = [f.noise_ratio for f in signals.frontal if f.noise_ratio is not None]
    if noises and min(noises) < limits.min_noise_ratio:
        hits.append(Hit(RiskSignal.NOISE_MISMATCH, round(min(noises), 4), limits.min_noise_ratio))
    if signals.parallax is not None and signals.parallax < limits.min_parallax:
        hits.append(Hit(RiskSignal.PERSPECTIVE_FLAT, signals.parallax, limits.min_parallax))
    return hits


def _pace_hits(pace: PaceOutcome) -> list[Hit]:
    signal = PACE_SIGNALS.get(pace.verdict)
    return [] if signal is None else [Hit(signal, pace.value, pace.threshold)]


def protocol_hits(signals: AttemptSignals, limits: SecurityThresholds) -> list[Hit]:
    """Las señales del protocolo de captura de un intento (reglas puras sobre lo ya medido; sin consultas)."""
    hits = _physical_hits(signals, limits)
    if signals.burst is not None:
        hits.extend(_burst_hits(signals.burst, limits))
    if signals.pace is not None:
        hits.extend(_pace_hits(signals.pace))
    return hits
