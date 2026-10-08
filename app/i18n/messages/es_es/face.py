"""Mensajes de identidad en español de España (es-ES): solo lo que cambia respecto de es-MX (vocabulario de España;
`docs/i18n/glosario.md` §3). Lo demás se reutiliza de `es_mx` (regla 6 de la raíz: sin duplicación)."""

from app.i18n.messages.base import Messages
from app.i18n.messages.es_mx import face as es_mx

#: Solo las llaves cuyo texto cambia en España (un plural que cambia va completo, con todas sus formas).
OVERRIDES: Messages = {
    "ENROLLMENT_PHOTO_EXPIRED": "Tu foto inicial ha caducado. Hazla de nuevo.",
    "ENROLLMENT_PHOTO_REQUIRED": "Primero haz tu foto inicial",
    "FLASH_TOKEN_INVALID": "El destello de este reto ha caducado o no es válido. Pide otro reto.",
    "SIGNATURE_STALE": "La firma de este dispositivo ha caducado. Inténtalo de nuevo.",
    "VERIFICATION_LOCATION_INVALID": (
        "Tu ubicación no es válida o no es lo bastante precisa. Activa la ubicación precisa (GPS) e inténtalo de nuevo."
    ),
    "VERIFICATION_LOCATION_REQUIRED": (
        "Se necesita tu ubicación para verificar tu identidad. Permite el acceso a tu ubicación e inténtalo de nuevo."
    ),
    "VOICE_SESSION_EXPIRED": (
        "La verificación por voz ha caducado. Ábrela de nuevo: tus respuestas aceptadas se conservan."
    ),
    "VOICE_SESSION_STARTED": "Preguntas en vídeo listas",
}
MESSAGES: Messages = {**es_mx.MESSAGES, **OVERRIDES}
