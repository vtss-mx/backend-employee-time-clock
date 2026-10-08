"""Cobranza (ADMIN): planes, cargos, pagos, estado de cuenta y suspensión.

Textos en italiano (it-IT), traducidos del sentido de es-MX: misma llave, mismos `{parámetros}` y mismas formas de
plural en todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AMOUNT_DECIMALS": {
        "zero": "La valuta {currency} non ammette decimali",
        "one": "La valuta {currency} ammette {count} decimale",
        "other": "La valuta {currency} ammette {count} decimali",
    },
    "BILLING_ACCOUNT": "Fatturazione dell'azienda",
    "BILLING_COMPANIES": {
        "one": "{count} azienda",
        "other": "{count} aziende",
    },
    "BILLING_ESTIMATE": "Stima del periodo in corso",
    "BILLING_OVERVIEW": "Riepilogo della fatturazione",
    "BILLING_PLAN_NOT_FOUND": "L'azienda non ha un piano di fatturazione",
    "BILLING_PLAN_SAVED": "Piano di fatturazione salvato",
    "BILLING_PREVIEW": "Anteprima dell'addebito",
    "BILLING_REASON_REQUIRED": "Scrivi il motivo (almeno 5 caratteri)",
    "BILLING_STATEMENT": {
        "one": "{count} movimento",
        "other": "{count} movimenti",
    },
    "CHARGES_LISTED": {
        "one": "{count} addebito",
        "other": "{count} addebiti",
    },
    "CHARGE_ALREADY_VOID": "L'addebito era già annullato",
    "CHARGE_FOUND": "Addebito trovato",
    "CHARGE_NOT_FOUND": "Addebito non trovato",
    "CHARGE_VOIDED": "Addebito annullato",
    "COMPANY_ALREADY_SUSPENDED": "L'azienda è già sospesa",
    "COMPANY_NOT_SUSPENDED": "L'azienda non è sospesa",
    "COMPANY_REACTIVATED": "Azienda riattivata",
    "COMPANY_SUSPENDED_BY_ADMIN": "Azienda sospesa: le sue sessioni sono state chiuse",
    "CURRENCY_CHOOSE": "Scegli una valuta dall'elenco",
    "CURRENCY_LOCKED": "La valuta non può più essere cambiata: l'azienda ha addebiti o pagamenti in {currency}",
    "CURRENCY_MISMATCH": "Il pagamento deve essere in {currency}, la valuta dell'azienda",
    "DISCOUNT_CHARGES_REQUIRED": "Indica a quanti addebiti si applica lo sconto",
    "DISCOUNT_PERCENT_MAX": "La percentuale di sconto non può superare 100",
    "PAYMENTS_LISTED": {
        "one": "{count} pagamento",
        "other": "{count} pagamenti",
    },
    "PAYMENT_ALREADY_VOID": "Il pagamento era già annullato",
    "PAYMENT_DATE_IN_FUTURE": "La data del pagamento non può essere futura",
    "PAYMENT_METHOD_INVALID": "Scegli un metodo di pagamento dall'elenco",
    "PAYMENT_NOT_FOUND": "Pagamento non trovato",
    "PAYMENT_RECEIPT": "Ricevuta",
    "PAYMENT_REGISTERED": "Pagamento registrato",
    "PAYMENT_VOIDED": "Pagamento annullato",
    "PLAN_START_OUT_OF_RANGE": "L'inizio della fatturazione deve essere entro un anno da oggi",
    "RECEIPT_NOT_FOUND": "Il pagamento non ha una ricevuta",
    "RECEIPT_TOO_LARGE": "La ricevuta supera la dimensione massima di {size}",
    "RECEIPT_TYPE_NOT_ALLOWED": "La ricevuta deve essere un PDF o un'immagine (JPG, PNG o WEBP)",
    "STATEMENT_CHARGE": "Addebito {sequence} · dal {start} al {end}",
    "STATEMENT_PAYMENT": "Pagamento",
    "SUSPENSION_NON_PAYMENT_NOTE": {
        "one": "Addebito scaduto il {due_on} e non pagato dopo {count} giorno di tolleranza.",
        "other": "Addebito scaduto il {due_on} e non pagato dopo {count} giorni di tolleranza.",
    },
}
