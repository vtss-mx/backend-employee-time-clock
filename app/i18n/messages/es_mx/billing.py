"""Cobranza (ADMIN): planes, cargos, pagos, estado de cuenta y suspensión.

Textos en español de México (es-MX, el idioma por omisión): misma llave, mismos `{parámetros}` y mismas formas de plural
en los dos idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AMOUNT_DECIMALS": {
        "zero": "La moneda {currency} no admite decimales",
        "one": "La moneda {currency} admite {count} decimal",
        "other": "La moneda {currency} admite {count} decimales",
    },
    "BILLING_ACCOUNT": "Cobranza de la empresa",
    "BILLING_COMPANIES": {
        "one": "{count} empresa",
        "other": "{count} empresas",
    },
    "BILLING_ESTIMATE": "Estimación del periodo en curso",
    "BILLING_OVERVIEW": "Resumen de la cobranza",
    "BILLING_PLAN_NOT_FOUND": "La empresa no tiene plan de cobro",
    "BILLING_PLAN_SAVED": "Plan de cobro guardado",
    "BILLING_PREVIEW": "Vista previa del cobro",
    "BILLING_REASON_REQUIRED": "Escribe el motivo (al menos 5 caracteres)",
    "BILLING_STATEMENT": {
        "one": "{count} movimiento",
        "other": "{count} movimientos",
    },
    "CHARGES_LISTED": {
        "one": "{count} cargo",
        "other": "{count} cargos",
    },
    "CHARGE_ALREADY_VOID": "El cargo ya estaba anulado",
    "CHARGE_FOUND": "Cargo encontrado",
    "CHARGE_NOT_FOUND": "Cargo no encontrado",
    "CHARGE_VOIDED": "Cargo anulado",
    "COMPANY_ALREADY_SUSPENDED": "La empresa ya está suspendida",
    "COMPANY_NOT_SUSPENDED": "La empresa no está suspendida",
    "COMPANY_REACTIVATED": "Empresa reactivada",
    "COMPANY_SUSPENDED_BY_ADMIN": "Empresa suspendida: sus sesiones se cerraron",
    "CURRENCY_CHOOSE": "Elige una moneda de la lista",
    "CURRENCY_LOCKED": "La moneda ya no se puede cambiar: la empresa tiene cargos o pagos en {currency}",
    "CURRENCY_MISMATCH": "El pago debe ser en {currency}, la moneda de la empresa",
    "DISCOUNT_CHARGES_REQUIRED": "Indica en cuántos cargos aplica el descuento",
    "DISCOUNT_PERCENT_MAX": "El porcentaje de descuento no puede pasar de 100",
    "PAYMENTS_LISTED": {
        "one": "{count} pago",
        "other": "{count} pagos",
    },
    "PAYMENT_ALREADY_VOID": "El pago ya estaba anulado",
    "PAYMENT_DATE_IN_FUTURE": "La fecha del pago no puede ser futura",
    "PAYMENT_METHOD_INVALID": "Elige una forma de pago de la lista",
    "PAYMENT_NOT_FOUND": "Pago no encontrado",
    "PAYMENT_RECEIPT": "Comprobante",
    "PAYMENT_REGISTERED": "Pago registrado",
    "PAYMENT_VOIDED": "Pago anulado",
    "PLAN_START_OUT_OF_RANGE": "El inicio del cobro debe estar a menos de un año de hoy",
    "RECEIPT_NOT_FOUND": "El pago no tiene comprobante",
    "RECEIPT_TOO_LARGE": "El comprobante excede el tamaño máximo de {size}",
    "RECEIPT_TYPE_NOT_ALLOWED": "El comprobante debe ser un PDF o una imagen (JPG, PNG o WEBP)",
    "STATEMENT_CHARGE": "Cargo {sequence} · {start} al {end}",
    "STATEMENT_PAYMENT": "Pago",
    "SUSPENSION_NON_PAYMENT_NOTE": {
        "one": "Cargo vencido el {due_on} sin pagar después de {count} día de gracia.",
        "other": "Cargo vencido el {due_on} sin pagar después de {count} días de gracia.",
    },
}
