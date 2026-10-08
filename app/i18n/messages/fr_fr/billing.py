"""Cobranza (ADMIN): planes, cargos, pagos, estado de cuenta y suspensión.

Textos en francés de Francia (fr-FR, registro «vous»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AMOUNT_DECIMALS": {
        "zero": "La devise {currency} n'accepte pas de décimales",
        "one": "La devise {currency} accepte {count} décimale",
        "other": "La devise {currency} accepte {count} décimales",
    },
    "BILLING_ACCOUNT": "Facturation de l'entreprise",
    "BILLING_COMPANIES": {
        "one": "{count} entreprise",
        "other": "{count} entreprises",
    },
    "BILLING_ESTIMATE": "Estimation de la période en cours",
    "BILLING_OVERVIEW": "Résumé de la facturation",
    "BILLING_PLAN_NOT_FOUND": "L'entreprise n'a pas de forfait de facturation",
    "BILLING_PLAN_SAVED": "Forfait de facturation enregistré",
    "BILLING_PREVIEW": "Aperçu de la facturation",
    "BILLING_REASON_REQUIRED": "Saisissez le motif (au moins 5 caractères)",
    "BILLING_STATEMENT": {
        "one": "{count} opération",
        "other": "{count} opérations",
    },
    "CHARGES_LISTED": {
        "one": "{count} échéance",
        "other": "{count} échéances",
    },
    "CHARGE_ALREADY_VOID": "L'échéance était déjà annulée",
    "CHARGE_FOUND": "Échéance trouvée",
    "CHARGE_NOT_FOUND": "Échéance introuvable",
    "CHARGE_VOIDED": "Échéance annulée",
    "COMPANY_ALREADY_SUSPENDED": "L'entreprise est déjà suspendue",
    "COMPANY_NOT_SUSPENDED": "L'entreprise n'est pas suspendue",
    "COMPANY_REACTIVATED": "Entreprise réactivée",
    "COMPANY_SUSPENDED_BY_ADMIN": "Entreprise suspendue: ses sessions ont été fermées",
    "CURRENCY_CHOOSE": "Choisissez une devise dans la liste",
    "CURRENCY_LOCKED": (
        "La devise ne peut plus être modifiée: l'entreprise a des échéances ou des paiements en {currency}"
    ),
    "CURRENCY_MISMATCH": "Le paiement doit être en {currency}, la devise de l'entreprise",
    "DISCOUNT_CHARGES_REQUIRED": "Indiquez à combien d'échéances s'applique la remise",
    "DISCOUNT_PERCENT_MAX": "Le pourcentage de remise ne peut pas dépasser 100",
    "PAYMENTS_LISTED": {
        "one": "{count} paiement",
        "other": "{count} paiements",
    },
    "PAYMENT_ALREADY_VOID": "Le paiement était déjà annulé",
    "PAYMENT_DATE_IN_FUTURE": "La date du paiement ne peut pas être dans le futur",
    "PAYMENT_METHOD_INVALID": "Choisissez un mode de paiement dans la liste",
    "PAYMENT_NOT_FOUND": "Paiement introuvable",
    "PAYMENT_RECEIPT": "Justificatif",
    "PAYMENT_REGISTERED": "Paiement enregistré",
    "PAYMENT_VOIDED": "Paiement annulé",
    "PLAN_START_OUT_OF_RANGE": "Le début de la facturation doit se situer à moins d'un an d'aujourd'hui",
    "RECEIPT_NOT_FOUND": "Le paiement n'a pas de justificatif",
    "RECEIPT_TOO_LARGE": "Le justificatif dépasse la taille maximale de {size}",
    "RECEIPT_TYPE_NOT_ALLOWED": "Le justificatif doit être un PDF ou une image (JPG, PNG ou WEBP)",
    "STATEMENT_CHARGE": "Échéance {sequence} · du {start} au {end}",
    "STATEMENT_PAYMENT": "Paiement",
    "SUSPENSION_NON_PAYMENT_NOTE": {
        "one": "Échéance du {due_on} impayée après {count} jour de grâce.",
        "other": "Échéance du {due_on} impayée après {count} jours de grâce.",
    },
}
