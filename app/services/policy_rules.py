"""Reglas puras de la política de verificación: qué cambio la RELAJA y los niveles predefinidos (presets).

Decisión D12 del dueño del producto: endurecer aplica al momento; relajar (bajar un umbral, apagar un candado,
dar más tiempo, una acción menos estricta) espera la aprobación de OTRO ADMIN (`policy_service`). Aquí vive la
dirección "más segura" de cada campo, sin base de datos ni configuración (se prueba sola):

- interruptores de protección: encendido es más seguro (salvo el QR, que abre un camino sin rostro);
- números: más es más seguro (confianza, calidad, movimientos, minutos de bloqueo) o menos lo es (tiempo del reto,
  intentos antes del bloqueo, vida del QR, precisión exigida, velocidad creíble, sospecha de duplicado y los cortes
  del puntaje de riesgo);
- catálogos con orden (anti-spoofing, destello, dispositivo del empleado, acciones de riesgo, modo de cada señal y
  de las pruebas de presencia de la fase 2b):
  un `sort_order` mayor es más estricto (así lo ordena su catálogo);
- lo que no protege ni desprotege (aprendizaje, evidencia, el nombre del preset) es neutral: se aplica al momento.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

#: Encendido = más seguro.
SAFER_WHEN_ON = frozenset(
    {
        "block_glasses",
        "block_headwear",
        "block_mask",
        "liveness_challenge",
        "anti_spoofing",
        "validator_mobile_only",
        "detect_impossible_travel",
        "block_virtual_cameras",
        "reject_foreign_images",
        "detect_static_captures",
        "detect_replays",
        "check_capture_continuity",
        "enforce_human_timing",
        "detect_duplicate_faces",
        "lockout_enabled",
        "validator_device_approval",
        "risk_engine",
        # Protocolo de captura (antifraude 2a): apagarlo quita la prueba en tiempo real y la ráfaga.
        "flash_paced",
        "capture_burst",
        # Verificación por voz y video del registro facial: apagarla deja el registro solo con las fotos.
        "voice_verification",
    }
)
#: Apagado = más seguro (caminos sin rostro).
SAFER_WHEN_OFF = frozenset({"qr_enabled", "qr_only_attendance"})
#: Un número mayor es más seguro.
SAFER_WHEN_HIGHER = frozenset(
    {"min_confidence", "identify_confidence", "min_capture_quality", "liveness_steps", "lockout_minutes"}
)
#: Un número menor es más seguro.
SAFER_WHEN_LOWER = frozenset(
    {
        "liveness_timeout_seconds",
        "lockout_max_failures",
        "qr_lifetime_seconds",
        "max_location_accuracy_m",
        "max_travel_kmh",
        "duplicate_confidence",
        "risk_medium_score",
        "risk_high_score",
        "risk_critical_score",
    }
)
#: Campos que son un código de un catálogo con orden (mayor `sort_order` = más estricto) y su catálogo.
ORDERED = {
    "anti_spoofing_level": "antispoof_levels",
    "flash_liveness": "flash_modes",
    "employee_device_mode": "employee_device_modes",
    "risk_medium_action": "risk_actions",
    "risk_high_action": "risk_actions",
    "risk_critical_action": "risk_actions",
    "risk_fallback_action": "risk_actions",
    # Antifraude 2b: la firma y la ubicación de los validadores y el código de sitio usan los modos de una señal.
    "validator_signing": "signal_modes",
    "validator_location": "signal_modes",
    "site_codes": "signal_modes",
    # Ubicación de cada verificación (decisión del dueño, 2026-10-07): bajar de modo relaja (regla de dos personas).
    "verification_location": "signal_modes",
}
#: El modo de cada señal (risk_signals.<código>.mode) se ordena con su catálogo.
SIGNAL_MODES = "signal_modes"


@dataclass(frozen=True)
class FieldChange:
    """Un campo que cambia: antes → después y si relaja la seguridad."""

    field: str
    before: Any
    after: Any
    relaxes: bool

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "before": self.before, "after": self.after, "relaxes": self.relaxes}


#: Orden de un código en su catálogo (lo da quien llama: las reglas no leen la base).
type Rank = Callable[[str, str], int]


def relaxes(field: str, before: Any, after: Any, rank: Rank) -> bool:
    """¿Cambiar `field` de `before` a `after` baja la seguridad? (los campos neutrales nunca la bajan)."""
    if field in SAFER_WHEN_ON:
        return bool(before) and not after
    if field in SAFER_WHEN_OFF:
        return not before and bool(after)
    if field in SAFER_WHEN_HIGHER:
        return float(after) < float(before)
    if field in SAFER_WHEN_LOWER:
        return float(after) > float(before)
    if field in ORDERED:
        return rank(ORDERED[field], str(after)) < rank(ORDERED[field], str(before))
    if field.startswith("risk_signals."):
        if field.endswith(".mode"):
            return rank(SIGNAL_MODES, str(after)) < rank(SIGNAL_MODES, str(before))
        return int(after) < int(before)  # puntos de la señal
    return False


def diff(current: Mapping[str, Any], wanted: Mapping[str, Any], rank: Rank) -> list[FieldChange]:
    """Los campos que de verdad cambian, cada uno con su dirección (en el orden en que se pidieron)."""
    return [
        FieldChange(field, current[field], value, relaxes(field, current[field], value, rank))
        for field, value in wanted.items()
        if current[field] != value
    ]


def flatten_signals(signals: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """`{"FLASH_FLAT": {"mode": "ENFORCE", "points": 30}}` → `{"risk_signals.FLASH_FLAT.mode": "ENFORCE", ...}`: cada
    ajuste de una señal es un campo del historial (antes → después) con su propia dirección."""
    return {
        f"risk_signals.{code}.{key}": value
        for code, setting in sorted(signals.items())
        for key, value in sorted(setting.items())
    }


# ---------------------------------------------------------------- niveles predefinidos (docs/rd §5.3)

_COMMON: dict[str, Any] = {
    "liveness_challenge": True,
    "anti_spoofing": True,
    "block_virtual_cameras": True,
    "reject_foreign_images": True,
    "detect_static_captures": True,
    "detect_replays": True,
    "check_capture_continuity": True,
    "enforce_human_timing": True,
    "detect_duplicate_faces": True,
    "lockout_enabled": True,
    "detect_impossible_travel": True,
    "validator_device_approval": True,
    "validator_mobile_only": True,
    "qr_only_attendance": False,
    "risk_engine": True,
    "risk_fallback_action": "ALLOW",
    # Antifraude 2a: los tres niveles usan la ráfaga de captura (medir no bloquea a nadie); solo Máximo la exige (sus
    # señales, abajo). El destello dictado se retiró con el destello (decisión del dueño, 2026-10-06): apagado.
    "flash_paced": False,
    "capture_burst": True,
    # Verificación por voz y video del registro facial (decisión del dueño, 2026-10-06): en los tres niveles.
    "voice_verification": True,
}
#: Antifraude 2b (decisión del dueño, 2026-10-06): en Estándar y Alto las pruebas de presencia de los validadores y el
#: código de sitio solo miden; Máximo las exige.
_PRESENCE_MEASURED: dict[str, Any] = {
    "validator_signing": "OBSERVE",
    "validator_location": "OBSERVE",
    "site_codes": "OBSERVE",
}
_PRESENCE_REQUIRED: dict[str, Any] = {
    "validator_signing": "ENFORCE",
    "validator_location": "ENFORCE",
    "site_codes": "ENFORCE",
}

#: Lo que fija cada nivel; el resto de la política queda como estaba. Las señales que no se nombran vuelven a su
#: valor de la plataforma (catalog.risk_signals).
# Endurecimiento de los tres niveles (decisión del dueño, 2026-10-08: «activar todas las políticas y endurécelas un
# 100% más»). Se endurece lo que la arquitectura permite SIN rechazar a personas reales: se suben los candados y los
# umbrales hacia su extremo estricto en cada nivel, Máximo activa también el bloqueo de lentes (invierte el valor por
# omisión, que sigue en false fuera de este nivel), exige la ubicación de cada verificación y agrega la firma del
# validador como regla dura. NO se reactiva el destello (retirado, decisión 2026-10-06) ni se obliga una señal sin
# calibrar a negar (las de `CALIBRATING_SIGNALS`/`BROWSER_SIGNALS` quedan topadas en «en revisión» por `ask_only`:
# «lo nuevo nunca niega»). El detalle y el porqué de cada tope están en `docs/rd/blindaje-frontera-2026-10-08.md`.
PRESETS: dict[str, dict[str, Any]] = {
    "STANDARD": {
        **_COMMON,
        **_PRESENCE_MEASURED,
        "liveness_steps": 2,
        "liveness_timeout_seconds": 45,  # 2026-10-08: menos holgura (antes 60)
        # Destello retirado de la experiencia (decisión del dueño, 2026-10-06): apagado en los tres niveles.
        "flash_liveness": "OFF",
        "anti_spoofing_level": "STANDARD",
        "min_capture_quality": 0.5,  # 2026-10-08 (antes 0.4)
        "lockout_max_failures": 4,  # 2026-10-08 (antes 5)
        "lockout_minutes": 30,  # 2026-10-08 (antes 15)
        "employee_device_mode": "OBSERVE",
        "risk_medium_score": 25,  # 2026-10-08: cortes más bajos (antes 30/60/80)
        "risk_high_score": 55,
        "risk_critical_score": 75,
        "risk_medium_action": "STEP_UP",
        "risk_high_action": "REVIEW",
        "risk_critical_action": "DENY",
        "risk_signals": {},
    },
    "HIGH": {
        **_COMMON,
        **_PRESENCE_MEASURED,
        "liveness_steps": 3,
        "liveness_timeout_seconds": 35,  # 2026-10-08 (antes 45)
        "flash_liveness": "OFF",
        "anti_spoofing_level": "HIGH",
        "min_capture_quality": 0.65,  # 2026-10-08 (antes 0.55)
        "lockout_max_failures": 4,
        "lockout_minutes": 120,  # 2026-10-08 (antes 30)
        "employee_device_mode": "STEP_UP",
        "risk_medium_score": 20,  # 2026-10-08 (antes 25/50/75)
        "risk_high_score": 45,
        "risk_critical_score": 70,
        "risk_medium_action": "STEP_UP",
        "risk_high_action": "REVIEW",
        "risk_critical_action": "DENY",
        # 2026-10-08: Alto exige también el ataque conocido (regla dura: niega sola si coincide una huella de ataque).
        "risk_signals": {"REPLAY_PERCEPTUAL": {"mode": "ENFORCE"}, "KNOWN_ATTACK": {"mode": "ENFORCE"}},
    },
    "MAXIMUM": {
        **_COMMON,
        **_PRESENCE_REQUIRED,
        # 2026-10-08: el nivel Máximo activa también el bloqueo de lentes (el valor por omisión sigue en false fuera de
        # este nivel) y exige la ubicación de cada verificación (OBSERVE → ENFORCE).
        "block_glasses": True,
        "verification_location": "ENFORCE",
        "liveness_steps": 3,
        "liveness_timeout_seconds": 20,  # 2026-10-08: el mínimo del rango (antes 30)
        "flash_liveness": "OFF",
        "anti_spoofing_level": "MAXIMUM",
        "min_capture_quality": 0.9,  # 2026-10-08: el máximo del rango (antes 0.7)
        "lockout_max_failures": 3,
        "lockout_minutes": 1440,  # 2026-10-08: el máximo del rango, un día (antes 60)
        "qr_lifetime_seconds": 15,  # 2026-10-08: la vida mínima del QR
        "max_location_accuracy_m": 10,  # 2026-10-08: la precisión más exigente
        "max_travel_kmh": 30,  # 2026-10-08: el viaje creíble más estricto
        "employee_device_mode": "APPROVAL",
        "risk_medium_score": 15,  # 2026-10-08: los cortes más bajos (antes 20/40/70)
        "risk_high_score": 35,
        "risk_critical_score": 65,
        "risk_medium_action": "STEP_UP",
        "risk_high_action": "DENY",
        "risk_critical_action": "DENY",
        "risk_signals": {
            "REPLAY_PERCEPTUAL": {"mode": "ENFORCE"},
            "KNOWN_ATTACK": {"mode": "ENFORCE"},
            # 2026-10-08: la firma del validador alterada es regla dura (niega sola) también desde el nivel Máximo.
            "VALIDATOR_SIGNATURE_INVALID": {"mode": "ENFORCE"},
            # Antifraude 2a (docs/rd §5.3; decisión del dueño del 2026-10-06: Máximo exige la ráfaga): sin ráfaga,
            # 20 puntos (el corte medio de este nivel: un paso más). Pide más, nunca niega (ASK_ONLY_SIGNALS). Las
            # señales del destello (FLASH_*) ya no se exigen: el destello se retiró de la experiencia ese mismo día y
            # sin él nunca se miden.
            "BURST_MISSING": {"mode": "ENFORCE", "points": 20},
        },
    },
}
