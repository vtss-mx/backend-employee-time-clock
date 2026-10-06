"""Mediciones de un intento facial mientras se analiza (solo números).

Las piezas de `identity_core` anotan aquí lo que miden (probabilidad de rostro real, cuánto se movió la
persona en cada paso, respuesta al destello, tiempos) y `IdentityLog.record` lo guarda junto con el
intento en la bitácora (misma transacción) como una fila de `ops.face_attempt_metrics`. Con esas
filas la plataforma se ajusta sola (`face_security`).

Vive en una variable de contexto de la petición (`ContextVar`): cada petición tiene la suya (FastAPI
copia el contexto a su hilo), así ninguna pieza tiene que pasar el recolector de mano en mano y nada
queda compartido entre peticiones ni entre instancias. `take_challenge`, el primer candado de toda
captura facial, empieza uno nuevo; un flujo sin rostro (solo QR) no tiene recolector y no guarda nada.

Antifraude (migración 0062): también lleva lo que el motor de riesgo necesita del intento (la cámara que
informó el cliente, si el reto era de "un paso más" o de una empresa reforzada), su decisión (`assessment`,
que se guarda con la bitácora) y, solo en memoria durante la petición, los bytes de algunas capturas por si el
intento abre un caso de fraude y la empresa guarda evidencia (decisión D1: cifrados en el bucket).

Antifraude 1b (migración 0065): lo que informó el cliente (`client`: IP, navegador, telemetría y la prueba del
dispositivo), las calidades JPEG de sus capturas, el otro empleado más parecido (1:N en cada 1:1) y lo que el motor
supo del dispositivo y de la red (el registro del intento anota el dispositivo y la asistencia guarda la red).

Antifraude 2a (migración 0066): lo medido del protocolo de captura: la ráfaga de recortes (`burst`), el destello dictado
por el servidor (`pace`) y el paralaje de los giros (`parallax`); el moiré y el ruido viajan en cada frontal. Desde la
ráfaga de 36 recortes (decisión del dueño, 2026-10-06), también su consenso de identidad (`consensus`).
"""

import math
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.core.ip_intel import IpInfo
from app.facial_recognition import FaceAnalysis, LivenessAction
from app.facial_recognition.photometry import FlashResponse
from app.facial_recognition.pipeline import BurstAnalysis
from app.models import FaceAttemptMetric, RiskAssessment
from app.services.client_evidence import ClientEvidence, DeviceCheck

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


def _max(values: Sequence[float | None]) -> float | None:
    clean = _finite(values)
    return round(max(clean), 5) if clean else None


class PaceVerdict(StrEnum):
    """Cómo llegó el destello de un reto dictado por el servidor (`flash_pacing.verify`)."""

    PACED = "PACED"  # cada color, comprometido a tiempo
    UNPACED = "UNPACED"  # sin comprobante: el destello de siempre (respaldo sin canal) o un programa
    TIMING = "TIMING"  # comprobante válido, pero algún color fuera de su ventana
    MISMATCH = "MISMATCH"  # comprobante alterado, de otro reto o con otras imágenes


@dataclass(frozen=True)
class PaceOutcome:
    """El destello dictado de un intento: su veredicto, los colores que de verdad se pintaron (comprobante válido), la
    respuesta más lenta (ms) y lo que mide su señal (valor y umbral)."""

    verdict: PaceVerdict
    colors: tuple[str, ...] = ()
    slowest_ms: int | None = None
    value: float | None = None
    threshold: float | None = None


@dataclass(frozen=True)
class BurstOutcome:
    """La ráfaga de un intento que la pidió: lo analizado (None si no llegó), si llegó mal formada y el parecido de sus
    recortes con las frontales (la misma persona)."""

    analysis: BurstAnalysis | None = None
    malformed: bool = False
    anchor: float | None = None


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
    #: Nombre de la cámara que informó el cliente y si la política lo exige (bloquea cámaras virtuales).
    camera: str | None = None
    camera_required: bool = False
    #: El reto era de "un paso más" (riesgo medio) / la empresa estaba reforzada por ataques al emitirlo.
    step_up: bool = False
    reinforced: bool = False
    #: Capturas que pueden ir como evidencia si el intento abre un caso: (tipo, orden, bytes). Solo en memoria.
    evidence: list[tuple[str, int, bytes]] = field(default_factory=list)
    keep_evidence: bool = False
    #: La decisión del motor de riesgo (se guarda con la bitácora) y su motivo de negocio para la empresa.
    assessment: RiskAssessment | None = None
    review_reasons: tuple[str, ...] = ()
    #: Quién opera la cámara (su cuenta firma el reto del dispositivo) y lo que informó el cliente.
    actor_id: int | None = None
    client: ClientEvidence | None = None
    #: Calidad JPEG de cada captura del intento (`jpeg_tables.encoder_quality`).
    jpeg: tuple[int | None, ...] = ()
    #: 1:N en cada 1:1: el OTRO empleado de la empresa más parecido y su similitud (la de su captura menos parecida).
    rival: tuple[int, float] | None = None
    #: El dispositivo del empleado (`employee_devices.DeviceCheck`, lo anota el registro del intento) y la red de la IP.
    device: DeviceCheck | None = None
    network: IpInfo | None = None
    #: Antifraude 2a: la ráfaga (None si no se pidió), el destello dictado (None si no lo fue) y el paralaje de los
    #: giros y cabeceos (el menor; None sin uno medible).
    burst: BurstOutcome | None = None
    pace: PaceOutcome | None = None
    parallax: float | None = None
    #: Consenso de identidad de la ráfaga (2026-10-06): la mediana del parecido de sus mejores recortes quietos con las
    #: muestras de la persona (None si no se pudo medir: sin ráfaga o con muy pocos recortes con rostro).
    consensus: float | None = None

    def challenged(self, steps: int, issued_at: datetime, now: datetime) -> None:
        """El reto que se respondió: cuántos movimientos pedía y cuánto tardó la respuesta."""
        self.steps = steps
        self.response_seconds = (now - issued_at).total_seconds()

    def add_step(self, action: LivenessAction, analysis: FaceAnalysis) -> None:
        self.step_real.append(analysis.real_probability)
        self.moves.setdefault(STEP_FAMILY[action], []).append(analysis.step_value)

    def keep(self, kind: str, images: Sequence[bytes], limit: int) -> None:
        """Guarda en memoria hasta `limit` capturas de un tipo (FRONTAL, STEP, FLASH) por si abren un caso."""
        if not self.keep_evidence:
            return
        room = max(0, limit - len(self.evidence))
        self.evidence.extend((kind, position, image) for position, image in enumerate(images[:room]))

    def metric(
        self, company_id: int, *, success: bool, reason: str | None, log_id: int | None = None
    ) -> FaceAttemptMetric:
        """La fila de métricas del intento (números redondeados; sin imágenes ni plantillas), enlazada con su
        intento de la bitácora (decisión D7)."""
        flash = self.flash
        return FaceAttemptMetric(
            company_id=company_id,
            verification_log_id=log_id,
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
            flash_ratio=flash.face_ratio if flash else None,
            quality_mean=_mean([f.quality_score for f in self.frontal]),
            brightness_mean=_mean([f.brightness for f in self.frontal]),
            **self._protocol(),
        )

    def _protocol(self) -> dict[str, float | int | None]:
        """Los números del protocolo de captura (antifraude 2a): la ráfaga, las señales físicas de las frontales (el
        peor caso: el mayor moiré, el menor cociente de ruido), el paralaje, la respuesta más lenta del destello y el
        consenso de identidad de la ráfaga."""
        analysis = self.burst.analysis if self.burst is not None else None
        pulse = analysis.pulse if analysis is not None else None
        return {
            "burst_frames": analysis.frames if analysis is not None else None,
            "burst_motion": analysis.motion if analysis is not None else None,
            "burst_consensus": self.consensus,
            "pulse_snr": pulse.snr_db if pulse is not None else None,
            "moire": _max([f.moire for f in self.frontal]),
            "noise_ratio": _min([f.noise_ratio for f in self.frontal]),
            "parallax": self.parallax,
            "flash_pace_ms": self.pace.slowest_ms if self.pace is not None else None,
        }


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
