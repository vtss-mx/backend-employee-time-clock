"""Mensajes de la plataforma e integraciones en español de España (es-ES): solo lo que cambia respecto de es-MX
(vocabulario de España; `docs/i18n/glosario.md` §3). Lo demás se reutiliza de `es_mx` (regla 6 de la raíz: sin
duplicación)."""

from app.i18n.messages.base import Messages
from app.i18n.messages.es_mx import platform as es_mx

#: Solo las llaves cuyo texto cambia en España (un plural que cambia va completo, con todas sus formas).
OVERRIDES: Messages = {
    "API_DEVICE_KEY_INVALID": "La clave del dispositivo no es válida",
    "API_DEVICE_PROOF_REQUIRED": "Falta la prueba del dispositivo: su clave, el reto y la firma",
    "API_KEYS_LISTED": {
        "one": "{count} clave",
        "other": "{count} claves",
    },
    "API_KEY_ACCESS_DISABLED": "La empresa de esta clave no tiene acceso a la API",
    "API_KEY_COMPANY_INACTIVE": "La empresa de esta clave está desactivada",
    "API_KEY_COMPANY_SUSPENDED": "La empresa de esta clave está suspendida",
    "API_KEY_CREATED": "Clave creada",
    "API_KEY_EXPIRED": "Esta clave de la API ha caducado",
    "API_KEY_INVALID": "La clave de la API no es válida",
    "API_KEY_LIMIT": "Tu empresa ya tiene {count} claves sin revocar. Revoca las que no uses.",
    "API_KEY_NAME_REQUIRED": "Escribe un nombre para la clave",
    "API_KEY_NOT_FOUND": "Clave no encontrada",
    "API_KEY_REQUIRED": "Falta la clave de la API: envíala en la cabecera X-API-Key",
    "API_KEY_REVOKED": "Esta clave de la API ha sido revocada",
    "API_KEY_REVOKED_DONE": "Clave revocada",
    "API_KEY_REVOKED_ROTATE": "Una clave revocada no se puede rotar: crea una nueva",
    "API_KEY_ROTATED": "Clave rotada",
    "API_KEY_VALIDATORS_DISABLED": "La empresa de esta clave no tiene el módulo de validadores",
    "API_SCOPE_REQUIRED": "Esta clave no tiene el permiso «{scope}». Pídelo a tu empresa (Integraciones).",
    "CLIENT_ERROR_RECORDED": "Fallo registrado",
    "COMPANY_ADMIN_CREATED": "Administrador añadido",
    "FRAUD_CASE_NOTE": "Nota añadida",
    "INTEGRATION_COMPANY": "Empresa de la clave",
    "POLICY_CHANGE_EXPIRED": "Caducó sin aprobarse",
    "STORAGE_KEY_INVALID": "la clave de la cuenta de servicio no es válida",
    "STORAGE_KEY_NOT_MOUNTED": "la clave de la cuenta de servicio aún no está montada (archivo vacío)",
    "STORAGE_KEY_UNREADABLE": "no se pudo leer la clave de la cuenta de servicio ({error})",
    "STORAGE_PAYMENT_RECEIPTS": "Justificantes de pago",
    "VALIDATOR_LIMIT_REACHED": {
        "zero": "Tu empresa ya no tiene plazas para validadores. Pide más al administrador de la plataforma.",
        "one": (
            "Tu empresa ha llegado a su límite de {count} validador activo. Desactiva uno o pide más al administrador "
            "de la plataforma."
        ),
        "other": (
            "Tu empresa ha llegado a su límite de {count} validadores activos. Desactiva uno o pide más al "
            "administrador de la plataforma."
        ),
    },
}
MESSAGES: Messages = {**es_mx.MESSAGES, **OVERRIDES}
