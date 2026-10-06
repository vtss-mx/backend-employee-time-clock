"""Motor de riesgo: reglas puras (sin base de datos), las mismas en vivo y en la simulación.

Diseño en docs/rd/antifraude-identidad.md §4. Cada intento facial produce señales (`Hit`: código, valor medido y
umbral); cada señal tiene, por empresa, un modo (apagada, solo medir u obligatoria) y sus puntos (`RiskConfig`, que
arma `risk_engine` con el catálogo `risk_signals` y los ajustes de la política). La decisión es EXPLICABLE:

1. Reglas duras: una señal dura (reenvío perceptual, ataque conocido) en modo obligatorio NIEGA sin importar el
   puntaje (nivel crítico).
2. Puntaje 0-100: la suma de los puntos de las señales OBLIGATORIAS, con tope por familia (tipo de fraude:
   `RISK_FAMILY_MAX_POINTS`) para que una sola fuente ruidosa no decida sola. Las de "solo medir" se registran con
   sus puntos (para calibrar y simular) pero no suman.
3. Nivel (bajo/medio/alto/crítico) según los cortes de la empresa → la acción configurada para ese nivel
   (decisión D3 por omisión: medio = un paso más, alto = en revisión, crítico = negar). Un intento que ya superó
   el reto de "un paso más" no vuelve a pedirlo: se permite.
4. Las señales de la fase 1b (dispositivo, red, lugar, 1:N y telemetría del navegador; `ASK_ONLY_SIGNALS`) piden más
   como mucho: si sin ellas el intento no se negaría, con ellas queda "en revisión" (docs/rd §2.5: la telemetría del
   navegador nunca niega; las demás, hasta calibrarlas con el plan de §7.4 y una decisión del dueño).
5. El dispositivo del empleado (decisión D2): con el modo «Un paso más» o «Aprobación de la empresa», un dispositivo
   que no es de confianza (`DEVICE_NEW` o `DEVICE_KEY_MISSING`) pide como mínimo un paso más o "en revisión"
   (`DEVICE_FLOOR`), aunque su señal esté apagada o el motor no sume puntos. Nunca niega.
6. Los motivos (`Reason`) quedan ordenados por puntos para el ADMIN; a la empresa solo llega el motivo de negocio.
7. Antifraude 2a (motor `1.2.0`): las señales del protocolo de captura (ráfaga, destello dictado y señales físicas)
   entran a `ASK_ONLY_SIGNALS` hasta calibrarlas. El pulso por video (`MEASURE_ONLY_SIGNALS`) ni eso: se registra con su
   valor y nunca suma, aunque alguien lo pida obligatorio (docs/rd §8 P4: no se ha calibrado con capturas reales).
8. Antifraude 2b: las señales de la presencia del validador y del código de sitio también entran a `ASK_ONLY_SIGNALS`;
   la firma alterada del validador es una regla dura (exigida, niega sola) y también nace en «Solo medir».
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from app.models import EmployeeDeviceMode, RiskAction, RiskSignal, RiskTier, SignalMode

#: Versión de estas reglas (se guarda con cada decisión: la calibración no mezcla versiones incompatibles).
ENGINE_VERSION = "1.2.0"
#: Acciones que abren (o suman a) un caso de fraude para el ADMIN.
CASE_ACTIONS = frozenset({RiskAction.ALERT, RiskAction.REVIEW, RiskAction.DENY})
#: Las acciones de la menos a la más estricta (el orden del catálogo `risk_actions`).
ACTION_ORDER = tuple(RiskAction)
#: Telemetría del navegador (docs/rd §2.5): lo declara el cliente; como mucho pide más, nunca niega.
BROWSER_SIGNALS = frozenset(
    {
        RiskSignal.AUTOMATION,
        RiskSignal.VIRTUAL_CAMERA_PRESENT,
        RiskSignal.TRACK_INCONSISTENT,
        RiskSignal.FRAME_TIMING_SYNTHETIC,
        RiskSignal.SCREEN_INCOHERENT,
        RiskSignal.TELEMETRY_MISSING,
        RiskSignal.JPEG_TABLE_UNKNOWN,
    }
)
#: Fase 1b (dispositivo, red, lugar y 1:N): nada nuevo niega hasta calibrarlo (docs/rd §7.2 y §7.4).
CALIBRATING_SIGNALS = frozenset(
    {
        RiskSignal.DEVICE_NEW,
        RiskSignal.DEVICE_KEY_MISSING,
        RiskSignal.DEVICE_SHARED,
        RiskSignal.IDENTITY_MISMATCH,
        RiskSignal.NETWORK_HOSTING,
        RiskSignal.NETWORK_COUNTRY_MISMATCH,
        RiskSignal.NETWORK_JUMP,
        RiskSignal.LOCATION_STATIC,
        RiskSignal.LOCATION_JUMP,
        # Fase 2a: el protocolo de captura (ráfaga, destello dictado y señales físicas de las capturas).
        RiskSignal.BURST_MISSING,
        RiskSignal.BURST_DISCONTINUOUS,
        RiskSignal.BURST_FROZEN,
        RiskSignal.BURST_LOOP,
        RiskSignal.MOIRE_HIGH,
        RiskSignal.NOISE_MISMATCH,
        RiskSignal.PERSPECTIVE_FLAT,
        RiskSignal.PULSE_ABSENT,
        RiskSignal.FLASH_UNPACED,
        RiskSignal.FLASH_PACE_TIMING,
        RiskSignal.FLASH_PACE_MISMATCH,
        # Fase 2b: la presencia del validador (firma por petición y ubicación) y el código de sitio. La firma alterada
        # (VALIDATOR_SIGNATURE_INVALID) NO está aquí: es una regla dura (solo una app alterada la produce), aunque
        # también nace en «Solo medir».
        RiskSignal.VALIDATOR_UNSIGNED,
        RiskSignal.VALIDATOR_KEY_MISMATCH,
        RiskSignal.VALIDATOR_LOCATION_MISSING,
        RiskSignal.VALIDATOR_LOCATION_INACCURATE,
        RiskSignal.VALIDATOR_OUT_OF_ZONE,
        RiskSignal.SITE_CODE_MISSING,
        RiskSignal.SITE_CODE_INVALID,
    }
)
#: Señales que como mucho piden más (un paso más o "en revisión"): nunca son la razón de negar un intento.
ASK_ONLY_SIGNALS = BROWSER_SIGNALS | CALIBRATING_SIGNALS
#: Señales que SOLO se miden: se registran con su valor, nunca suman ni se pueden exigir (el pulso por video, hasta que
#: el dueño lo calibre con el plan de docs/rd §7.4).
MEASURE_ONLY_SIGNALS = frozenset({RiskSignal.PULSE_ABSENT})
#: Señales que dicen que el dispositivo del empleado no es de confianza para el modo de su empresa.
UNTRUSTED_DEVICE = frozenset({RiskSignal.DEVICE_NEW, RiskSignal.DEVICE_KEY_MISSING})
#: Lo mínimo que pide cada modo del dispositivo ante uno que no es de confianza (decisión D2; nunca niega).
DEVICE_FLOOR = {EmployeeDeviceMode.STEP_UP: RiskAction.STEP_UP, EmployeeDeviceMode.APPROVAL: RiskAction.REVIEW}
#: Motivo de negocio (catalog.review_reasons) de un registro "en revisión" por su dispositivo.
DEVICE_REVIEW_REASON = "DEVICE"


@dataclass(frozen=True)
class SignalSetting:
    """Una señal en una empresa: su familia (tipo de fraude), puntos, modo, si es regla dura y su motivo de negocio."""

    code: str
    kind: str
    points: int
    mode: str
    hard: bool = False
    review_reason: str = "ACTIVITY"


@dataclass(frozen=True)
class RiskConfig:
    """La configuración de riesgo de una empresa (política + catálogo)."""

    enabled: bool
    medium: int
    high: int
    critical: int
    medium_action: str
    high_action: str
    critical_action: str
    fallback: str
    family_cap: int
    #: Modo del dispositivo del empleado (catalog.employee_device_modes).
    device_mode: str = EmployeeDeviceMode.OFF
    signals: Mapping[str, SignalSetting] = field(default_factory=dict)

    def version(self) -> str:
        """Huella corta de la configuración (se guarda con cada decisión y permite agrupar por política)."""
        data = {**asdict(self), "signals": {code: asdict(s) for code, s in sorted(self.signals.items())}}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Hit:
    """Una señal que se disparó en el intento: lo medido y el umbral con que se comparó."""

    code: str
    value: float | None = None
    threshold: float | None = None


@dataclass(frozen=True)
class Reason:
    """Un motivo de la decisión (lo que se guarda en `ops.risk_assessments.reasons`)."""

    code: str
    points: int
    mode: str
    kind: str
    value: float | None = None
    threshold: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "points": self.points,
            "mode": self.mode,
            "kind": self.kind,
            "value": self.value,
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class Decision:
    score: int
    tier: RiskTier
    action: RiskAction
    reasons: tuple[Reason, ...]
    #: La regla dura que negó el intento (su código es el motivo de la bitácora), si la hubo.
    hard: str | None = None
    #: El modo del dispositivo de la empresa pidió más: el del intento no es de confianza (decisión D2).
    device: bool = False

    @property
    def enforced(self) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.mode == SignalMode.ENFORCE)


def tier_of(score: int, config: RiskConfig) -> RiskTier:
    if score >= config.critical:
        return RiskTier.CRITICAL
    if score >= config.high:
        return RiskTier.HIGH
    if score >= config.medium:
        return RiskTier.MEDIUM
    return RiskTier.LOW


def action_for(tier: RiskTier, config: RiskConfig) -> RiskAction:
    actions = {
        RiskTier.LOW: RiskAction.ALLOW,
        RiskTier.MEDIUM: RiskAction(config.medium_action),
        RiskTier.HIGH: RiskAction(config.high_action),
        RiskTier.CRITICAL: RiskAction(config.critical_action),
    }
    return actions[tier]


def mode_of(code: str, mode: str) -> str:
    """El modo con que cuenta una señal: una que solo se mide nunca pasa de «Solo medir» (apagada sigue apagada)."""
    return SignalMode.OBSERVE if code in MEASURE_ONLY_SIGNALS and mode == SignalMode.ENFORCE else mode


def reasons_of(hits: Iterable[Hit], config: RiskConfig) -> tuple[Reason, ...]:
    """Las señales disparadas con su configuración (las apagadas o desconocidas no cuentan), por puntos."""
    reasons = [
        Reason(hit.code, setting.points, mode_of(hit.code, setting.mode), setting.kind, hit.value, hit.threshold)
        for hit in hits
        if (setting := config.signals.get(hit.code)) is not None and setting.mode != SignalMode.OFF
    ]
    return tuple(sorted(reasons, key=lambda r: (-r.points, r.code)))


def score_of(reasons: Sequence[Reason], family_cap: int) -> int:
    """Suma de los puntos obligatorios, con tope por familia y en 0-100."""
    families: dict[str, int] = {}
    for reason in reasons:
        if reason.mode == SignalMode.ENFORCE:
            families[reason.kind] = families.get(reason.kind, 0) + reason.points
    return max(0, min(100, sum(min(family_cap, points) for points in families.values())))


def stricter(first: RiskAction, second: RiskAction | None) -> RiskAction:
    """La más estricta de dos acciones (`None` no pide nada)."""
    if second is None:
        return first
    return first if ACTION_ORDER.index(first) >= ACTION_ORDER.index(second) else second


def device_floor(hits: Sequence[Hit], config: RiskConfig) -> RiskAction | None:
    """Lo que exige el modo del dispositivo de la empresa si el del intento no es de confianza (o None)."""
    if not any(hit.code in UNTRUSTED_DEVICE for hit in hits):
        return None
    return DEVICE_FLOOR.get(EmployeeDeviceMode(config.device_mode))


def ask_only(action: RiskAction, reasons: Sequence[Reason], config: RiskConfig) -> RiskAction:
    """Si el intento se negaría solo por las señales que piden más como mucho, queda "en revisión"."""
    if action != RiskAction.DENY:
        return action
    rest = score_of([r for r in reasons if r.code not in ASK_ONLY_SIGNALS], config.family_cap)
    return action if action_for(tier_of(rest, config), config) == RiskAction.DENY else RiskAction.REVIEW


def decide(
    hits: Iterable[Hit], config: RiskConfig, *, step_up_done: bool = False, can_step_up: bool = True
) -> Decision:
    """La decisión de un intento (ver el docstring del módulo). `can_step_up=False`: el flujo no puede pedir otro
    reto (la empresa no usa prueba de vida): "un paso más" se vuelve "en revisión" (nadie se queda sin checar)."""
    hits = list(hits)
    reasons = reasons_of(hits, config)
    floor = device_floor(hits, config)
    score, tier, action = 0, RiskTier.LOW, RiskAction.ALLOW
    if config.enabled:
        hard = next((r for r in reasons if r.mode == SignalMode.ENFORCE and config.signals[r.code].hard), None)
        if hard is not None:
            return Decision(100, RiskTier.CRITICAL, RiskAction.DENY, reasons, hard=hard.code)
        score = score_of(reasons, config.family_cap)
        tier = tier_of(score, config)
        action = ask_only(action_for(tier, config), reasons, config)
    action = stricter(action, floor)
    if action == RiskAction.STEP_UP and step_up_done:
        action = RiskAction.ALLOW
    elif action == RiskAction.STEP_UP and not can_step_up:
        action = RiskAction.REVIEW
    return Decision(score, tier, action, reasons, device=floor is not None)


def review_reasons(decision: Decision, config: RiskConfig) -> tuple[str, ...]:
    """Los motivos de negocio (catalog.review_reasons) de las señales que decidieron, sin repetir y en orden de peso:
    es lo único que ve la empresa (nunca qué señal técnica lo delató)."""
    seen: list[str] = []
    for reason in decision.enforced:
        code = config.signals[reason.code].review_reason
        if code not in seen:
            seen.append(code)
    if decision.device and DEVICE_REVIEW_REASON not in seen:
        seen.append(DEVICE_REVIEW_REASON)
    return tuple(seen)


def replay(stored: Sequence[Mapping[str, Any]], config: RiskConfig, *, step_up_done: bool = False) -> Decision:
    """Vuelve a decidir un intento ya guardado (simulación) con OTRA configuración: sus motivos guardados son las
    señales que se dispararon (también las de "solo medir")."""
    hits = [Hit(str(r["code"]), r.get("value"), r.get("threshold")) for r in stored]
    return decide(hits, config, step_up_done=step_up_done)
