"""Mensajes de asistencia en español de España (es-ES): solo lo que cambia respecto de es-MX (vocabulario de España;
`docs/i18n/glosario.md` §3). Lo demás se reutiliza de `es_mx` (regla 6 de la raíz: sin duplicación)."""

from app.i18n.messages.base import Messages
from app.i18n.messages.es_mx import attendance as es_mx

#: Solo las llaves cuyo texto cambia en España (un plural que cambia va completo, con todas sus formas).
OVERRIDES: Messages = {
    "ABSENCE_OVERLAP": "Se solapa con su ausencia «{kind}» ({state}) del {start} al {end}",
    "BREAKS_OVERLAP": "Los descansos no se pueden solapar",
    "DEVICE_KEY_INVALID": "La clave de este dispositivo no es válida. Recarga la página e inténtalo de nuevo.",
    "HOLIDAY_CREATED": "Día festivo añadido",
    "KIOSKS": {
        "one": "{count} quiosco",
        "other": "{count} quioscos",
    },
    "KIOSK_CREATED": "Quiosco registrado",
    "KIOSK_DELETED": "Quiosco eliminado",
    "KIOSK_NOT_FOUND": "Quiosco no encontrado",
    "KIOSK_PAIRED": "Quiosco vinculado",
    "KIOSK_PAIRING_INVALID": "El código de vinculación no es válido o ha caducado. Pide uno nuevo a tu empresa.",
    "KIOSK_PROOF_INVALID": "No se pudo verificar este quiosco. Inténtalo de nuevo.",
    "KIOSK_PROOF_REQUIRED": "Falta verificar este quiosco. Inténtalo de nuevo.",
    "KIOSK_RESTORED": "Quiosco restaurado",
    "KIOSK_UNPAIRED": "Este quiosco ya no está vinculado. Pide a tu empresa un código de vinculación nuevo.",
    "LOCATION_OUT_OF_SITE": "Hoy debes fichar en tu sitio de trabajo.",
    "LOCATION_OUT_OF_SITE_NEAREST": "Hoy debes fichar en tu sitio de trabajo. Estás a {distance} de {site}.",
    "LOCATION_SAMPLES_INVALID": (
        "Las lecturas de la ubicación no son válidas: envía como máximo {max}, cada una con latitud, longitud y "
        "precisión."
    ),
    "NEXT_SHIFT": "Tu siguiente turno es el {day} a las {start}; puedes fichar desde las {opens}.",
    "NO_CHECK_IN_FOR_CHECK_OUT": "No tienes una entrada registrada para fichar tu salida",
    "OFFICIAL_HOLIDAYS_ADDED": {
        "one": "{count} festivo oficial añadido",
        "other": "{count} festivos oficiales añadidos",
    },
    "SITE_CODE_REQUIRED": "Para fichar en {site}, escanea el código del quiosco del sitio.",
    "SITE_CODE_UNAVAILABLE": "No se pudo generar el código del sitio. Inténtalo de nuevo en unos minutos.",
    "SITE_CODE_USED": "Ya has usado este código. Espera el siguiente en el quiosco.",
    "SITE_REQUIRED": "Elige al menos un sitio para fichar los días no remotos",
}
MESSAGES: Messages = {**es_mx.MESSAGES, **OVERRIDES}
