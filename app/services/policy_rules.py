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
    # Antifraude 2a: los tres niveles usan el protocolo de captura (medir no bloquea a nadie); solo Máximo lo exige
    # (sus señales, abajo).
    "flash_paced": True,
    "capture_burst": True,
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
PRESETS: dict[str, dict[str, Any]] = {
    "STANDARD": {
        **_COMMON,
        **_PRESENCE_MEASURED,
        "liveness_steps": 2,
        "liveness_timeout_seconds": 60,
        # "Obligatorio tras calibrar": mientras no haya datos, solo se mide (decisión del dueño sobre el destello).
        "flash_liveness": "OBSERVE",
        "anti_spoofing_level": "STANDARD",
        "min_capture_quality": 0.4,
        "lockout_max_failures": 5,
        "lockout_minutes": 15,
        "employee_device_mode": "OBSERVE",
        "risk_medium_score": 30,
        "risk_high_score": 60,
        "risk_critical_score": 80,
        "risk_medium_action": "STEP_UP",
        "risk_high_action": "REVIEW",
        "risk_critical_action": "DENY",
        "risk_signals": {},
    },
    "HIGH": {
        **_COMMON,
        **_PRESENCE_MEASURED,
        "liveness_steps": 3,
        "liveness_timeout_seconds": 45,
        "flash_liveness": "ENFORCE",
        "anti_spoofing_level": "HIGH",
        "min_capture_quality": 0.55,
        "lockout_max_failures": 4,
        "lockout_minutes": 30,
        "employee_device_mode": "STEP_UP",
        "risk_medium_score": 25,
        "risk_high_score": 50,
        "risk_critical_score": 75,
        "risk_medium_action": "STEP_UP",
        "risk_high_action": "REVIEW",
        "risk_critical_action": "DENY",
        "risk_signals": {"REPLAY_PERCEPTUAL": {"mode": "ENFORCE"}},
    },
    "MAXIMUM": {
        **_COMMON,
        **_PRESENCE_REQUIRED,
        "liveness_steps": 3,
        "liveness_timeout_seconds": 30,
        "flash_liveness": "ENFORCE",
        "anti_spoofing_level": "MAXIMUM",
        "min_capture_quality": 0.7,
        "lockout_max_failures": 3,
        "lockout_minutes": 60,
        "employee_device_mode": "APPROVAL",
        "risk_medium_score": 20,
        "risk_high_score": 40,
        "risk_critical_score": 70,
        "risk_medium_action": "STEP_UP",
        "risk_high_action": "DENY",
        "risk_critical_action": "DENY",
        "risk_signals": {
            "REPLAY_PERCEPTUAL": {"mode": "ENFORCE"},
            "KNOWN_ATTACK": {"mode": "ENFORCE"},
            "FLASH_FLAT": {"mode": "ENFORCE"},
            # Antifraude 2a (docs/rd §5.3: «obligatorio y dictado por el servidor» + ráfaga; decisión del dueño del
            # 2026-10-06: Máximo los exige): sin destello dictado o sin ráfaga, 20 puntos (el corte medio de este nivel:
            # un paso más); un color fuera de tiempo, sus 15; alterado, 35. Piden más, nunca niegan (ASK_ONLY_SIGNALS):
            # una red que bloquea el canal en vivo sigue pudiendo checar tras el paso extra.
            "FLASH_UNPACED": {"mode": "ENFORCE", "points": 20},
            "FLASH_PACE_TIMING": {"mode": "ENFORCE"},
            "BURST_MISSING": {"mode": "ENFORCE", "points": 20},
            "FLASH_PACE_MISMATCH": {"mode": "ENFORCE"},
        },
    },
}
