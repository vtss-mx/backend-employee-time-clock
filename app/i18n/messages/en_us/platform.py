"""Plataforma (ADMIN) e integraciones: empresas, política de verificación, errores, rendimiento, consumo, llaves de la
API y casos de fraude.

Textos en inglés de Estados Unidos (en-US): misma llave, mismos `{parámetros}` y mismas formas de plural
en los dos idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ACCOUNT_DELETED_LABEL": "Account #{id} (no longer exists)",
    "ACCOUNT_LABEL": "{email} ({role})",
    "API_KEYS_LISTED": {
        "one": "{count} key",
        "other": "{count} keys",
    },
    "API_KEY_ACCESS_DISABLED": "This key's company doesn't have access to the API",
    "API_KEY_COMPANY_INACTIVE": "This key's company is deactivated",
    "API_KEY_COMPANY_SUSPENDED": "This key's company is suspended",
    "API_KEY_CREATED": "Key created",
    "API_KEY_EXPIRED": "This API key expired",
    "API_KEY_INVALID": "The API key isn't valid",
    "API_KEY_LIMIT": "Your company already has {count} unrevoked keys. Revoke the ones you don't use.",
    "API_KEY_NAME_REQUIRED": "Enter a name for the key",
    "API_KEY_NOT_FOUND": "Key not found",
    "API_KEY_REQUIRED": "The API key is missing: send it in the X-API-Key header",
    "API_KEY_REVOKED": "This API key was revoked",
    "API_KEY_REVOKED_DONE": "Key revoked",
    "API_KEY_REVOKED_ROTATE": "A revoked key can't be rotated: create a new one",
    "API_KEY_ROTATED": "Key rotated",
    "API_KEY_VALIDATORS_DISABLED": "This key's company doesn't have the validators module",
    "API_SCOPE_INVALID": "Invalid permissions: {scopes}",
    "API_SCOPE_REQUIRED": "This key doesn't have the “{scope}” permission. Ask your company for it (Integrations).",
    "ATTENDANCE_FEED": {
        "one": "{count} identification",
        "other": "{count} identifications",
    },
    "ATTENDANCE_LISTED": {
        "one": "{count} identification",
        "other": "{count} identifications",
    },
    "CASE_STATUS_FILTER_INVALID": "Choose one of the available case statuses",
    "CLIENT_ERROR_RECORDED": "Failure recorded",
    "COMPANIES_LISTED": {
        "one": "{count} company found",
        "other": "{count} companies found",
    },
    "COMPANY_ADMINS_LISTED": {
        "one": "{count} administrator",
        "other": "{count} administrators",
    },
    "COMPANY_ADMIN_CREATED": "Administrator added",
    "COMPANY_ADMIN_FOUND": "Administrator found",
    "COMPANY_ADMIN_PASSWORD_RESET": "Password reset",
    "COMPANY_ADMIN_STATUS_UPDATED": "Administrator updated",
    "COMPANY_CREATED": "Company registered",
    "COMPANY_DELETED": "Company deleted",
    "COMPANY_EMPLOYEES": {
        "one": "{count} employee",
        "other": "{count} employees",
    },
    "COMPANY_FOUND": "Company found",
    "COMPANY_RESTORED": "Company restored",
    "COMPANY_UPDATED": "Company updated",
    "COMPANY_USAGE": "Company usage",
    "ERRORS_RESOLVED": {
        "one": "{count} error resolved",
        "other": "{count} errors resolved",
    },
    "ERROR_FILTER_REQUIRED": "Filter by status or severity to mark errors as resolved",
    "ERROR_FILTER_RESOLVED": "Those errors are already resolved",
    "ERROR_OCCURRENCES_LISTED": {
        "one": "{count} occurrence",
        "other": "{count} occurrences",
    },
    "ERROR_REPORTS_LISTED": {
        "one": "{count} error",
        "other": "{count} errors",
    },
    "ERROR_REPORT_FOUND": "Error found",
    "ERROR_REPORT_NOT_FOUND": "Error not found",
    "ERROR_STATUS_UPDATED": "Follow-up updated",
    "ERROR_SUMMARY": "Error summary",
    "FACE_LEARNING_FORGOTTEN": {
        "zero": "No learned samples: it's compared only with their approved enrollment",
        "one": "Learning reset ({count} sample): it's compared only with their approved enrollment",
        "other": "Learning reset ({count} samples): it's compared only with their approved enrollment",
    },
    "FACE_LEARNING_SUMMARY": "Face recognition progress",
    "FRAUD_CASE": "Fraud case",
    "FRAUD_CASES": {
        "one": "{count} case",
        "other": "{count} cases",
    },
    "FRAUD_CASES_COUNT": "Active fraud cases",
    "FRAUD_CASE_DECIDED": "Case updated",
    "FRAUD_CASE_NOTE": "Note added",
    "FRAUD_CASE_NOT_FOUND": "Fraud case not found",
    "FRAUD_CASE_SAME_STATUS": "The case already has that status",
    "FRAUD_EVIDENCE": "Evidence",
    "FRAUD_EVIDENCE_NOT_FOUND": "The evidence is no longer available",
    "FRAUD_NOTE_REQUIRED": "Explain why you're confirming or dismissing the fraud",
    "FRAUD_NOTE_TEXT_REQUIRED": "Write the note",
    "INTEGRATION_COMPANY": "The key's company",
    "INVALID_ANTISPOOF_LEVEL": "Choose one of the available anti-spoofing levels",
    "INVALID_CASE_STATUS": "Choose the case's new status",
    "INVALID_CONFIDENCE_LEVEL": "Choose one of the available confidence levels",
    "INVALID_CURSOR": "The cursor isn't valid",
    "INVALID_DEVICE_MODE": "Choose one of the available device modes",
    "INVALID_FLASH_MODE": "Choose one of the available flash modes",
    "INVALID_POLICY_PRESET": "Choose one of the preset levels",
    "INVALID_RANGE": "The start date can't be after the end date",
    "INVALID_RISK_ACTION": "Choose one of the available actions",
    "INVALID_RISK_FALLBACK": "If the engine fails, choose allow, alert, or ask for one more step",
    "INVALID_RISK_SIGNAL": "That risk signal doesn't exist",
    "INVALID_SIGNAL_MODE": "Choose one of the signal's modes",
    "INVALID_SINCE_UNTIL": "`since` must be before `until`",
    "PERFORMANCE_METRICS": {
        "one": "{count} item",
        "other": "{count} items",
    },
    "PERFORMANCE_OVERVIEW": "Platform performance",
    "PERFORMANCE_SERIES": "Performance series",
    "PERFORMANCE_STATEMENTS": {
        "one": "{count} query",
        "other": "{count} queries",
    },
    "PERFORMANCE_WEB_VITALS": {
        "one": "{count} screen",
        "other": "{count} screens",
    },
    "PLATFORM_STATS": "Platform indicators",
    "POLICY": "Verification policy",
    "POLICY_CHANGED_SINCE": "The policy changed after the request: the change must be requested again",
    "POLICY_CHANGES": {
        "one": "{count} change",
        "other": "{count} changes",
    },
    "POLICY_CHANGE_APPROVED": "Change approved: the policy now applies it",
    "POLICY_CHANGE_CANCELLED": "Change canceled",
    "POLICY_CHANGE_EXPIRED": "It expired without approval",
    "POLICY_CHANGE_NOT_FOUND": "Policy change not found",
    "POLICY_CHANGE_NOT_PENDING": "This change was already decided",
    "POLICY_CHANGE_PENDING": "This change lowers security: it will apply once another admin approves it",
    "POLICY_CHANGE_REJECTED": "Change rejected",
    "POLICY_NOT_REQUESTER": "Only the person who requested the change can cancel it",
    "POLICY_SELF_APPROVAL": "You can't approve your own change: another admin must do it",
    "POLICY_SELF_DECISION": "You can't reject your own change: use “Withdraw”",
    "POLICY_SIMULATED": {
        "one": "{count} attempt simulated",
        "other": "{count} attempts simulated",
    },
    "POLICY_UNCHANGED": "No changes to the policy",
    "POLICY_UPDATED": "Verification policy updated",
    "RANGE_TOO_LONG": "Choose a range of up to one year",
    "RESTORE_COMPANY_TAX_ID_TAKEN": "Can't restore: another company already has that tax ID ({name} {value})",
    "RISK_SCORES_ORDER": "The risk cutoffs must be in order: medium < high < critical",
    "SERVER_STATUS": "Server status",
    "SIGNAL_MEASURE_ONLY": "This signal is measured only: it can't be required until it's calibrated",
    "SLOW_ALERTS_LISTED": {
        "one": "{count} alert",
        "other": "{count} alerts",
    },
    "SLOW_ALERTS_SUMMARY": "Alert summary",
    "SLOW_ALERT_FOUND": "Alert found",
    "SLOW_ALERT_NOT_FOUND": "Alert not found",
    "SLOW_ALERT_STATUS_UPDATED": "Follow-up updated",
    "THRESHOLDS_RECALIBRATED": {
        "zero": "Thresholds recalculated: no changes",
        "one": "Thresholds recalculated ({count} changed)",
        "other": "Thresholds recalculated ({count} changed)",
    },
    "TOO_MANY_SAMPLES": "A batch can have up to {count} samples",
    "USAGE_COMPANIES": {
        "one": "{count} company",
        "other": "{count} companies",
    },
    "USAGE_OVERVIEW": "Platform usage",
    "USAGE_ROUTES": {
        "one": "{count} route",
        "other": "{count} routes",
    },
    "USAGE_USERS": {
        "one": "{count} account",
        "other": "{count} accounts",
    },
    "VALIDATORS_DISABLED": "Your company doesn't have the validators module. Ask the platform administrator for it.",
    "VALIDATORS_LISTED": {
        "one": "{count} validator",
        "other": "{count} validators",
    },
    "VALIDATOR_LIMIT_BELOW_ACTIVE": {
        "one": "The company has {count} active validator: the limit can't be lower. Ask them to deactivate it first.",
        "other": (
            "The company has {count} active validators: the limit can't be lower. Ask them to deactivate the extra "
            "ones."
        ),
    },
    "VALIDATOR_LIMIT_REACHED": {
        "zero": "Your company has no validator seats left. Ask the platform administrator for more.",
        "one": (
            "Your company reached its limit of {count} active validator. Deactivate one or ask the platform "
            "administrator for more."
        ),
        "other": (
            "Your company reached its limit of {count} active validators. Deactivate one or ask the platform "
            "administrator for more."
        ),
    },
    "WEB_PERFORMANCE_RECORDED": "Performance recorded",
}
