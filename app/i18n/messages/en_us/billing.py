"""Cobranza (ADMIN): planes, cargos, pagos, estado de cuenta y suspensión.

Textos en inglés de Estados Unidos (en-US): misma llave, mismos `{parámetros}` y mismas formas de plural
en los dos idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AMOUNT_DECIMALS": {
        "zero": "The {currency} currency doesn't allow decimals",
        "one": "The {currency} currency allows {count} decimal place",
        "other": "The {currency} currency allows {count} decimal places",
    },
    "BILLING_ACCOUNT": "Company billing",
    "BILLING_COMPANIES": {
        "one": "{count} company",
        "other": "{count} companies",
    },
    "BILLING_ESTIMATE": "Estimate for the current period",
    "BILLING_OVERVIEW": "Billing summary",
    "BILLING_PLAN_NOT_FOUND": "The company doesn't have a billing plan",
    "BILLING_PLAN_SAVED": "Billing plan saved",
    "BILLING_PREVIEW": "Billing preview",
    "BILLING_REASON_REQUIRED": "Write the reason (at least 5 characters)",
    "BILLING_STATEMENT": {
        "one": "{count} transaction",
        "other": "{count} transactions",
    },
    "CHARGES_LISTED": {
        "one": "{count} charge",
        "other": "{count} charges",
    },
    "CHARGE_ALREADY_VOID": "The charge was already voided",
    "CHARGE_FOUND": "Charge found",
    "CHARGE_NOT_FOUND": "Charge not found",
    "CHARGE_VOIDED": "Charge voided",
    "COMPANY_ALREADY_SUSPENDED": "The company is already suspended",
    "COMPANY_NOT_SUSPENDED": "The company isn't suspended",
    "COMPANY_REACTIVATED": "Company reactivated",
    "COMPANY_SUSPENDED_BY_ADMIN": "Company suspended: its sessions were closed",
    "CURRENCY_CHOOSE": "Choose a currency from the list",
    "CURRENCY_LOCKED": "The currency can no longer be changed: the company has charges or payments in {currency}",
    "CURRENCY_MISMATCH": "The payment must be in {currency}, the company's currency",
    "DISCOUNT_CHARGES_REQUIRED": "Enter how many charges the discount applies to",
    "DISCOUNT_PERCENT_MAX": "The discount percentage can't be more than 100",
    "PAYMENTS_LISTED": {
        "one": "{count} payment",
        "other": "{count} payments",
    },
    "PAYMENT_ALREADY_VOID": "The payment was already voided",
    "PAYMENT_DATE_IN_FUTURE": "The payment date can't be in the future",
    "PAYMENT_METHOD_INVALID": "Choose a payment method from the list",
    "PAYMENT_NOT_FOUND": "Payment not found",
    "PAYMENT_RECEIPT": "Receipt",
    "PAYMENT_REGISTERED": "Payment recorded",
    "PAYMENT_VOIDED": "Payment voided",
    "PLAN_START_OUT_OF_RANGE": "Billing must start within one year of today",
    "RECEIPT_NOT_FOUND": "The payment doesn't have a receipt",
    "RECEIPT_TOO_LARGE": "The receipt is larger than the maximum size of {size}",
    "RECEIPT_TYPE_NOT_ALLOWED": "The receipt must be a PDF or an image (JPG, PNG, or WEBP)",
    "STATEMENT_CHARGE": "Charge {sequence} · {start} to {end}",
    "STATEMENT_PAYMENT": "Payment",
    "SUSPENSION_NON_PAYMENT_NOTE": {
        "one": "Charge due on {due_on} still unpaid after {count} grace day.",
        "other": "Charge due on {due_on} still unpaid after {count} grace days.",
    },
}
