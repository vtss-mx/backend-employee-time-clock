"""Mensajes de facturación en español de España (es-ES): solo lo que cambia respecto de es-MX (vocabulario de España;
`docs/i18n/glosario.md` §3). Lo demás se reutiliza de `es_mx` (regla 6 de la raíz: sin duplicación)."""

from app.i18n.messages.base import Messages
from app.i18n.messages.es_mx import billing as es_mx

#: Solo las llaves cuyo texto cambia en España (un plural que cambia va completo, con todas sus formas).
OVERRIDES: Messages = {
    "BILLING_ACCOUNT": "Facturación de la empresa",
    "BILLING_OVERVIEW": "Resumen de la facturación",
    "DISCOUNT_CHARGES_REQUIRED": "Indica en cuántos cargos se aplica el descuento",
    "PAYMENT_RECEIPT": "Justificante",
    "RECEIPT_NOT_FOUND": "El pago no tiene justificante",
    "RECEIPT_TOO_LARGE": "El justificante excede el tamaño máximo de {size}",
    "RECEIPT_TYPE_NOT_ALLOWED": "El justificante debe ser un PDF o una imagen (JPG, PNG o WEBP)",
}
MESSAGES: Messages = {**es_mx.MESSAGES, **OVERRIDES}
