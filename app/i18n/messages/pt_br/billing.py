"""Cobranza (ADMIN): planes, cargos, pagos, estado de cuenta y suspensión.

Textos en portugués de Brasil (pt-BR, trato de «você»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`); la forma `one` también sirve para el 0 (CLDR). Un cargo es una «cobrança» y los
días de gracia son «dias de carência», como en la cobranza brasileña."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AMOUNT_DECIMALS": {
        "zero": "A moeda {currency} não aceita casas decimais",
        "one": "A moeda {currency} aceita {count} casa decimal",
        "other": "A moeda {currency} aceita {count} casas decimais",
    },
    "BILLING_ACCOUNT": "Cobrança da empresa",
    "BILLING_COMPANIES": {
        "one": "{count} empresa",
        "other": "{count} empresas",
    },
    "BILLING_ESTIMATE": "Estimativa do período em andamento",
    "BILLING_OVERVIEW": "Resumo da cobrança",
    "BILLING_PLAN_NOT_FOUND": "A empresa não tem plano de cobrança",
    "BILLING_PLAN_SAVED": "Plano de cobrança salvo",
    "BILLING_PREVIEW": "Prévia da cobrança",
    "BILLING_REASON_REQUIRED": "Escreva o motivo (pelo menos 5 caracteres)",
    "BILLING_STATEMENT": {
        "one": "{count} lançamento",
        "other": "{count} lançamentos",
    },
    "CHARGES_LISTED": {
        "one": "{count} cobrança",
        "other": "{count} cobranças",
    },
    "CHARGE_ALREADY_VOID": "A cobrança já estava anulada",
    "CHARGE_FOUND": "Cobrança encontrada",
    "CHARGE_NOT_FOUND": "Cobrança não encontrada",
    "CHARGE_VOIDED": "Cobrança anulada",
    "COMPANY_ALREADY_SUSPENDED": "A empresa já está suspensa",
    "COMPANY_NOT_SUSPENDED": "A empresa não está suspensa",
    "COMPANY_REACTIVATED": "Empresa reativada",
    "COMPANY_SUSPENDED_BY_ADMIN": "Empresa suspensa: as sessões dela foram encerradas",
    "CURRENCY_CHOOSE": "Escolha uma moeda da lista",
    "CURRENCY_LOCKED": "A moeda não pode mais ser alterada: a empresa tem cobranças ou pagamentos em {currency}",
    "CURRENCY_MISMATCH": "O pagamento deve ser em {currency}, a moeda da empresa",
    "DISCOUNT_CHARGES_REQUIRED": "Informe em quantas cobranças o desconto se aplica",
    "DISCOUNT_PERCENT_MAX": "A porcentagem de desconto não pode passar de 100",
    "PAYMENTS_LISTED": {
        "one": "{count} pagamento",
        "other": "{count} pagamentos",
    },
    "PAYMENT_ALREADY_VOID": "O pagamento já estava anulado",
    "PAYMENT_DATE_IN_FUTURE": "A data do pagamento não pode ser futura",
    "PAYMENT_METHOD_INVALID": "Escolha uma forma de pagamento da lista",
    "PAYMENT_NOT_FOUND": "Pagamento não encontrado",
    "PAYMENT_RECEIPT": "Comprovante",
    "PAYMENT_REGISTERED": "Pagamento registrado",
    "PAYMENT_VOIDED": "Pagamento anulado",
    "PLAN_START_OUT_OF_RANGE": "O início da cobrança deve estar a menos de um ano de hoje",
    "RECEIPT_NOT_FOUND": "O pagamento não tem comprovante",
    "RECEIPT_TOO_LARGE": "O comprovante excede o tamanho máximo de {size}",
    "RECEIPT_TYPE_NOT_ALLOWED": "O comprovante deve ser um PDF ou uma imagem (JPG, PNG ou WEBP)",
    "STATEMENT_CHARGE": "Cobrança {sequence} · {start} a {end}",
    "STATEMENT_PAYMENT": "Pagamento",
    "SUSPENSION_NON_PAYMENT_NOTE": {
        "one": "Cobrança vencida em {due_on} sem pagamento após {count} dia de carência.",
        "other": "Cobrança vencida em {due_on} sem pagamento após {count} dias de carência.",
    },
}
