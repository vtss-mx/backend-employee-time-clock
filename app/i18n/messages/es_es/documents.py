"""Mensajes de documentos de la empresa en español de España (es-ES): solo lo que cambia respecto de es-MX
(vocabulario de España; `docs/i18n/glosario.md` §3). Lo demás se reutiliza de `es_mx` (regla 6 de la raíz: sin
duplicación)."""

from app.i18n.messages.base import Messages
from app.i18n.messages.es_mx import documents as es_mx

#: Solo las llaves cuyo texto cambia en España (un plural que cambia va completo, con todas sus formas).
OVERRIDES: Messages = {
    "DOCUMENT_MACROS_NOT_ALLOWED": "El archivo tiene macros. Guárdalo sin macros (DOCX o XLSX) e inténtalo de nuevo.",
}
MESSAGES: Messages = {**es_mx.MESSAGES, **OVERRIDES}
