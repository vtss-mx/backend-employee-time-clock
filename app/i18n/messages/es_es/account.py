"""Mensajes de la cuenta en español de España (es-ES): solo lo que cambia respecto de es-MX (vocabulario de España;
`docs/i18n/glosario.md` §3). Lo demás se reutiliza de `es_mx` (regla 6 de la raíz: sin duplicación)."""

from app.i18n.messages.base import Messages
from app.i18n.messages.es_mx import account as es_mx

#: Solo las llaves cuyo texto cambia en España (un plural que cambia va completo, con todas sus formas).
OVERRIDES: Messages = {
    "AUTH_BUSY": "Hay muchos inicios de sesión en este momento. Inténtalo de nuevo en unos segundos.",
    "COMPANY_INACTIVE": "Tu empresa está desactivada en la plataforma. Contacta con el administrador.",
    "COMPANY_SELECTED": "Has entrado en {company}",
    "COMPANY_SUSPENDED": (
        "Tu empresa está suspendida. Contacta con el administrador de la plataforma para reactivarla."
    ),
    "DEVICE_DESKTOP": "Ordenador",
    "DEVICE_INVALID_TRANSITION": "Ese cambio no se puede aplicar al estado actual del dispositivo",
    "DEVICE_LOCATION_INACCURATE": (
        "La ubicación de tu dispositivo no es precisa (±{accuracy}). Activa la ubicación precisa o el GPS e "
        "inténtalo de nuevo."
    ),
    "JWKS": "Claves públicas de firma",
    "LOCATION_ACCURACY_MISSING": (
        "Tu dispositivo no indicó la precisión de tu ubicación. Activa la ubicación precisa o el GPS e inténtalo de "
        "nuevo."
    ),
    "PASSKEYS_LISTED": {
        "one": "{count} clave de acceso",
        "other": "{count} claves de acceso",
    },
    "PASSKEY_ALREADY_REGISTERED": "Esa clave de acceso ya está registrada",
    "PASSKEY_CHALLENGE_INVALID": "El reto de la clave de acceso caducó o no es válido. Inténtalo de nuevo.",
    "PASSKEY_CHALLENGE_USED": "Ese reto ya se usó. Inténtalo de nuevo.",
    "PASSKEY_CLONED": (
        "Esa clave de acceso se usó desde una copia y se revocó por seguridad. Entra con tu contraseña y registra una "
        "nueva."
    ),
    "PASSKEY_INVALID": "No se pudo verificar la clave de acceso que envió tu dispositivo. Inténtalo de nuevo.",
    "PASSKEY_LIMIT_REACHED": {
        "one": "Ya tienes {count} clave de acceso. Revoca una para registrar otra.",
        "other": "Ya tienes {count} claves de acceso. Revoca una para registrar otra.",
    },
    "PASSKEY_LOGIN_FAILED": "No se pudo entrar con esa clave de acceso. Inténtalo de nuevo o usa tu contraseña.",
    "PASSKEY_LOGIN_OPTIONS": "Reto para entrar con una clave de acceso",
    "PASSKEY_NOT_FOUND": "Clave de acceso no encontrada",
    "PASSKEY_OPTIONS": "Reto para registrar una clave de acceso",
    "PASSKEY_REGISTERED": "Clave de acceso registrada",
    "PASSKEY_RENAMED": "Nombre de la clave guardado",
    "PASSKEY_REVOKED": "Clave de acceso revocada",
}
MESSAGES: Messages = {**es_mx.MESSAGES, **OVERRIDES}
