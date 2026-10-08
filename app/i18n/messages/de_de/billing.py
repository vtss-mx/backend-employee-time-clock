"""Cobranza (ADMIN): planes, cargos, pagos, estado de cuenta y suspensión.

Textos en alemán de Alemania (de-DE, trato de «Sie»): misma llave, mismos `{parámetros}` y mismas formas de plural
en todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AMOUNT_DECIMALS": {
        "zero": "Die Währung {currency} erlaubt keine Dezimalstellen",
        "one": "Die Währung {currency} erlaubt {count} Dezimalstelle",
        "other": "Die Währung {currency} erlaubt {count} Dezimalstellen",
    },
    "BILLING_ACCOUNT": "Abrechnung des Unternehmens",
    "BILLING_COMPANIES": {
        "one": "{count} Unternehmen",
        "other": "{count} Unternehmen",
    },
    "BILLING_ESTIMATE": "Schätzung für den laufenden Zeitraum",
    "BILLING_OVERVIEW": "Übersicht der Abrechnung",
    "BILLING_PLAN_NOT_FOUND": "Das Unternehmen hat keinen Abrechnungstarif",
    "BILLING_PLAN_SAVED": "Abrechnungstarif gespeichert",
    "BILLING_PREVIEW": "Vorschau der Abrechnung",
    "BILLING_REASON_REQUIRED": "Geben Sie den Grund an (mindestens 5 Zeichen)",
    "BILLING_STATEMENT": {
        "one": "{count} Buchung",
        "other": "{count} Buchungen",
    },
    "CHARGES_LISTED": {
        "one": "{count} Gebühr",
        "other": "{count} Gebühren",
    },
    "CHARGE_ALREADY_VOID": "Die Gebühr war bereits storniert",
    "CHARGE_FOUND": "Gebühr gefunden",
    "CHARGE_NOT_FOUND": "Gebühr nicht gefunden",
    "CHARGE_VOIDED": "Gebühr storniert",
    "COMPANY_ALREADY_SUSPENDED": "Das Unternehmen ist bereits gesperrt",
    "COMPANY_NOT_SUSPENDED": "Das Unternehmen ist nicht gesperrt",
    "COMPANY_REACTIVATED": "Unternehmen wieder aktiviert",
    "COMPANY_SUSPENDED_BY_ADMIN": "Unternehmen gesperrt: Alle Sitzungen wurden beendet",
    "CURRENCY_CHOOSE": "Wählen Sie eine Währung aus der Liste",
    "CURRENCY_LOCKED": (
        "Die Währung kann nicht mehr geändert werden: Das Unternehmen hat Gebühren oder Zahlungen in {currency}"
    ),
    "CURRENCY_MISMATCH": "Die Zahlung muss in {currency} erfolgen, der Währung des Unternehmens",
    "DISCOUNT_CHARGES_REQUIRED": "Geben Sie an, auf wie viele Gebühren der Rabatt angewendet wird",
    "DISCOUNT_PERCENT_MAX": "Der Rabatt darf höchstens 100 Prozent betragen",
    "PAYMENTS_LISTED": {
        "one": "{count} Zahlung",
        "other": "{count} Zahlungen",
    },
    "PAYMENT_ALREADY_VOID": "Die Zahlung war bereits storniert",
    "PAYMENT_DATE_IN_FUTURE": "Das Zahlungsdatum darf nicht in der Zukunft liegen",
    "PAYMENT_METHOD_INVALID": "Wählen Sie eine Zahlungsart aus der Liste",
    "PAYMENT_NOT_FOUND": "Zahlung nicht gefunden",
    "PAYMENT_RECEIPT": "Beleg",
    "PAYMENT_REGISTERED": "Zahlung erfasst",
    "PAYMENT_VOIDED": "Zahlung storniert",
    "PLAN_START_OUT_OF_RANGE": "Der Beginn der Abrechnung muss weniger als ein Jahr von heute entfernt sein",
    "RECEIPT_NOT_FOUND": "Für die Zahlung gibt es keinen Beleg",
    "RECEIPT_TOO_LARGE": "Der Beleg überschreitet die maximale Größe von {size}",
    "RECEIPT_TYPE_NOT_ALLOWED": "Der Beleg muss eine PDF-Datei oder ein Bild sein (JPG, PNG oder WEBP)",
    "STATEMENT_CHARGE": "Gebühr {sequence} · {start} bis {end}",
    "STATEMENT_PAYMENT": "Zahlung",
    "SUSPENSION_NON_PAYMENT_NOTE": {
        "one": "Gebühr fällig am {due_on}, nach {count} Tag Nachfrist nicht bezahlt.",
        "other": "Gebühr fällig am {due_on}, nach {count} Tagen Nachfrist nicht bezahlt.",
    },
}
