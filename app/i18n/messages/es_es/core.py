"""Mensajes generales en español de España (es-ES): solo lo que cambia respecto de es-MX (vocabulario de España;
`docs/i18n/glosario.md` §3). Lo demás se reutiliza de `es_mx` (regla 6 de la raíz: sin duplicación)."""

from app.i18n.messages.base import Messages
from app.i18n.messages.es_mx import core as es_mx

#: Solo las llaves cuyo texto cambia en España (un plural que cambia va completo, con todas sus formas).
OVERRIDES: Messages = {
    "CONCURRENT_UPDATE": "Otra operación cambió estos datos al mismo tiempo. Inténtalo de nuevo.",
    "CONTENT_LENGTH_INVALID": "La cabecera `Content-Length` no es válida",
    "DATABASE_TIMEOUT": "La operación tardó demasiado. Inténtalo de nuevo en unos segundos.",
    "DATABASE_UNAVAILABLE": "Servicio no disponible. Inténtalo de nuevo en unos segundos.",
    "IDENTIFICATION_FAILED": "No se pudo confirmar tu identidad. Inténtalo de nuevo.",
    "IMAGE_NOT_PROCESSED": "No se pudo procesar la imagen. Inténtalo de nuevo.",
    "INPUT_TOO_FEW": {
        "one": "Añade al menos {count} elemento",
        "other": "Añade al menos {count} elementos",
    },
    "INTERNAL_ERROR": "Se ha producido un error inesperado. Inténtalo de nuevo.",
    "INVALID_CHARACTERS": "El texto tiene caracteres que no se permiten. Quítalos e inténtalo de nuevo.",
    "RATE_LIMITED": "Demasiadas solicitudes. Inténtalo de nuevo en unos segundos.",
    "SERVER_BUSY": "El servicio está saturado. Inténtalo de nuevo en unos segundos.",
    "SERVICE_UNAVAILABLE": "Servicio no disponible. Inténtalo de nuevo en unos segundos.",
    "STORAGE_UNAVAILABLE": "El almacenamiento de imágenes no está disponible. Inténtalo de nuevo en unos minutos.",
    "WS_TOO_MANY_CONNECTIONS": "Demasiadas conexiones. Inténtalo de nuevo en unos segundos.",
}
MESSAGES: Messages = {**es_mx.MESSAGES, **OVERRIDES}
