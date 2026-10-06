"""Plataforma (ADMIN) e integraciones: empresas, política de verificación, errores, rendimiento, consumo, llaves de la
API y casos de fraude.

Textos en español de México (es-MX, el idioma por omisión): misma llave, mismos `{parámetros}` y mismas formas de plural
en los dos idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ACCOUNT_DELETED_LABEL": "Cuenta #{id} (ya no existe)",
    "ACCOUNT_LABEL": "{email} ({role})",
    "API_KEYS_LISTED": {
        "one": "{count} llave",
        "other": "{count} llaves",
    },
    "API_KEY_ACCESS_DISABLED": "La empresa de esta llave no tiene acceso a la API",
    "API_KEY_COMPANY_INACTIVE": "La empresa de esta llave está desactivada",
    "API_KEY_COMPANY_SUSPENDED": "La empresa de esta llave está suspendida",
    "API_KEY_CREATED": "Llave creada",
    "API_KEY_EXPIRED": "Esta llave de la API venció",
    "API_KEY_INVALID": "La llave de la API no es válida",
    "API_KEY_LIMIT": "Tu empresa ya tiene {count} llaves sin revocar. Revoca las que no uses.",
    "API_KEY_NAME_REQUIRED": "Escribe un nombre para la llave",
    "API_KEY_NOT_FOUND": "Llave no encontrada",
    "API_KEY_REQUIRED": "Falta la llave de la API: envíala en la cabecera X-API-Key",
    "API_KEY_REVOKED": "Esta llave de la API fue revocada",
    "API_KEY_REVOKED_DONE": "Llave revocada",
    "API_KEY_REVOKED_ROTATE": "Una llave revocada no se puede rotar: crea una nueva",
    "API_KEY_ROTATED": "Llave rotada",
    "API_KEY_VALIDATORS_DISABLED": "La empresa de esta llave no tiene el módulo de validadores",
    "API_SCOPE_INVALID": "Permisos no válidos: {scopes}",
    "API_SCOPE_REQUIRED": "Esta llave no tiene el permiso «{scope}». Pídelo a tu empresa (Integraciones).",
    "ATTENDANCE_FEED": {
        "one": "{count} identificación",
        "other": "{count} identificaciones",
    },
    "ATTENDANCE_LISTED": {
        "one": "{count} identificación",
        "other": "{count} identificaciones",
    },
    "CASE_STATUS_FILTER_INVALID": "Elige uno de los estados de caso disponibles",
    "CLIENT_ERROR_RECORDED": "Falla registrada",
    "COMPANIES_LISTED": {
        "one": "{count} empresa encontrada",
        "other": "{count} empresas encontradas",
    },
    "COMPANY_ADMINS_LISTED": {
        "one": "{count} administrador",
        "other": "{count} administradores",
    },
    "COMPANY_ADMIN_CREATED": "Administrador agregado",
    "COMPANY_ADMIN_FOUND": "Administrador encontrado",
    "COMPANY_ADMIN_PASSWORD_RESET": "Contraseña restablecida",
    "COMPANY_ADMIN_STATUS_UPDATED": "Administrador actualizado",
    "COMPANY_CREATED": "Empresa registrada",
    "COMPANY_DELETED": "Empresa eliminada",
    "COMPANY_EMPLOYEES": {
        "one": "{count} empleado",
        "other": "{count} empleados",
    },
    "COMPANY_FOUND": "Empresa encontrada",
    "COMPANY_RESTORED": "Empresa restaurada",
    "COMPANY_UPDATED": "Empresa actualizada",
    "COMPANY_USAGE": "Consumo de la empresa",
    "ERRORS_RESOLVED": {
        "one": "{count} error solucionado",
        "other": "{count} errores solucionados",
    },
    "ERROR_FILTER_REQUIRED": "Filtra por estado o gravedad para marcar errores como solucionados",
    "ERROR_FILTER_RESOLVED": "Esos errores ya están solucionados",
    "ERROR_OCCURRENCES_LISTED": {
        "one": "{count} ocurrencia",
        "other": "{count} ocurrencias",
    },
    "ERROR_REPORTS_LISTED": {
        "one": "{count} error",
        "other": "{count} errores",
    },
    "ERROR_REPORT_FOUND": "Error encontrado",
    "ERROR_REPORT_NOT_FOUND": "Error no encontrado",
    "ERROR_STATUS_UPDATED": "Seguimiento actualizado",
    "ERROR_SUMMARY": "Resumen de errores",
    "FACE_LEARNING_FORGOTTEN": {
        "zero": "Sin muestras aprendidas: se compara solo con su registro aprobado",
        "one": "Aprendizaje reiniciado ({count} muestra): se compara solo con su registro aprobado",
        "other": "Aprendizaje reiniciado ({count} muestras): se compara solo con su registro aprobado",
    },
    "FACE_LEARNING_SUMMARY": "Evolución del reconocimiento facial",
    "FRAUD_CASE": "Caso de fraude",
    "FRAUD_CASES": {
        "one": "{count} caso",
        "other": "{count} casos",
    },
    "FRAUD_CASES_COUNT": "Casos de fraude activos",
    "FRAUD_CASE_DECIDED": "Caso actualizado",
    "FRAUD_CASE_NOTE": "Nota agregada",
    "FRAUD_CASE_NOT_FOUND": "Caso de fraude no encontrado",
    "FRAUD_CASE_SAME_STATUS": "El caso ya está en ese estado",
    "FRAUD_EVIDENCE": "Evidencia",
    "FRAUD_EVIDENCE_NOT_FOUND": "La evidencia ya no está disponible",
    "FRAUD_NOTE_REQUIRED": "Explica por qué confirmas o descartas el fraude",
    "FRAUD_NOTE_TEXT_REQUIRED": "Escribe la nota",
    "INTEGRATION_COMPANY": "Empresa de la llave",
    "INVALID_ANTISPOOF_LEVEL": "Elige uno de los niveles de anti-spoofing disponibles",
    "INVALID_CASE_STATUS": "Elige cómo queda el caso",
    "INVALID_CONFIDENCE_LEVEL": "Elige uno de los niveles de confianza disponibles",
    "INVALID_CURSOR": "El cursor no es válido",
    "INVALID_DEVICE_MODE": "Elige uno de los modos del dispositivo disponibles",
    "INVALID_FLASH_MODE": "Elige uno de los modos del destello disponibles",
    "INVALID_POLICY_PRESET": "Elige uno de los niveles predefinidos",
    "INVALID_RANGE": "La fecha inicial no puede ser posterior a la final",
    "INVALID_RISK_ACTION": "Elige una de las acciones disponibles",
    "INVALID_RISK_FALLBACK": "Si el motor falla, elige permitir, avisar o pedir un paso más",
    "INVALID_RISK_SIGNAL": "Esa señal de riesgo no existe",
    "INVALID_SIGNAL_MODE": "Elige uno de los modos de la señal",
    "INVALID_SINCE_UNTIL": "`since` debe ser anterior a `until`",
    "PERFORMANCE_METRICS": {
        "one": "{count} elemento",
        "other": "{count} elementos",
    },
    "PERFORMANCE_OVERVIEW": "Rendimiento de la plataforma",
    "PERFORMANCE_SERIES": "Serie del rendimiento",
    "PERFORMANCE_STATEMENTS": {
        "one": "{count} consulta",
        "other": "{count} consultas",
    },
    "PERFORMANCE_WEB_VITALS": {
        "one": "{count} pantalla",
        "other": "{count} pantallas",
    },
    "PLATFORM_STATS": "Indicadores de la plataforma",
    "POLICY": "Política de verificación",
    "POLICY_CHANGED_SINCE": "La política cambió después de la solicitud: hay que pedir el cambio de nuevo",
    "POLICY_CHANGES": {
        "one": "{count} cambio",
        "other": "{count} cambios",
    },
    "POLICY_CHANGE_APPROVED": "Cambio aprobado: la política ya lo aplica",
    "POLICY_CHANGE_CANCELLED": "Cambio cancelado",
    "POLICY_CHANGE_EXPIRED": "Venció sin aprobarse",
    "POLICY_CHANGE_NOT_FOUND": "Cambio de política no encontrado",
    "POLICY_CHANGE_NOT_PENDING": "Este cambio ya se decidió",
    "POLICY_CHANGE_PENDING": "El cambio relaja la seguridad: se aplicará cuando otro administrador lo apruebe",
    "POLICY_CHANGE_REJECTED": "Cambio rechazado",
    "POLICY_NOT_REQUESTER": "Solo quien pidió el cambio puede cancelarlo",
    "POLICY_SELF_APPROVAL": "No puedes aprobar tu propio cambio: otro administrador debe hacerlo",
    "POLICY_SELF_DECISION": "No puedes rechazar tu propio cambio: usa «Retirar»",
    "POLICY_SIMULATED": {
        "one": "{count} intento simulado",
        "other": "{count} intentos simulados",
    },
    "POLICY_UNCHANGED": "Sin cambios en la política",
    "POLICY_UPDATED": "Política de verificación actualizada",
    "RANGE_TOO_LONG": "Elige un rango de hasta un año",
    "RESTORE_COMPANY_TAX_ID_TAKEN": (
        "No se puede restaurar: otra empresa ya tiene ese identificador fiscal ({name} {value})"
    ),
    "RISK_SCORES_ORDER": "Los cortes del riesgo deben ir en orden: medio < alto < crítico",
    "SERVER_STATUS": "Estado del servidor",
    "SIGNAL_MEASURE_ONLY": "Esta señal solo se mide: no se puede exigir hasta calibrarla",
    "SLOW_ALERTS_LISTED": {
        "one": "{count} alerta",
        "other": "{count} alertas",
    },
    "SLOW_ALERTS_SUMMARY": "Resumen de alertas",
    "SLOW_ALERT_FOUND": "Alerta encontrada",
    "SLOW_ALERT_NOT_FOUND": "Alerta no encontrada",
    "SLOW_ALERT_STATUS_UPDATED": "Seguimiento actualizado",
    "THRESHOLDS_RECALIBRATED": {
        "zero": "Umbrales recalculados: sin cambios",
        "one": "Umbrales recalculados ({count} cambió)",
        "other": "Umbrales recalculados ({count} cambiaron)",
    },
    "TOO_MANY_SAMPLES": "Un lote admite hasta {count} muestras",
    "USAGE_COMPANIES": {
        "one": "{count} empresa",
        "other": "{count} empresas",
    },
    "USAGE_OVERVIEW": "Consumo de la plataforma",
    "USAGE_ROUTES": {
        "one": "{count} ruta",
        "other": "{count} rutas",
    },
    "USAGE_USERS": {
        "one": "{count} cuenta",
        "other": "{count} cuentas",
    },
    "VALIDATORS_DISABLED": (
        "Tu empresa no tiene el módulo de validadores. Solicítalo al administrador de la plataforma."
    ),
    "VALIDATORS_LISTED": {
        "one": "{count} validador",
        "other": "{count} validadores",
    },
    "VALIDATOR_LIMIT_BELOW_ACTIVE": {
        "one": (
            "La empresa tiene {count} validador activo: el límite no puede ser menor. Pídele que lo desactive primero."
        ),
        "other": (
            "La empresa tiene {count} validadores activos: el límite no puede ser menor. Pídele que desactive los que "
            "sobran."
        ),
    },
    "VALIDATOR_LIMIT_REACHED": {
        "zero": "Tu empresa ya no tiene lugares para validadores. Pide más al administrador de la plataforma.",
        "one": (
            "Tu empresa llegó a su límite de {count} validador activo. Desactiva uno o pide más al administrador de la "
            "plataforma."
        ),
        "other": (
            "Tu empresa llegó a su límite de {count} validadores activos. Desactiva uno o pide más al administrador de "
            "la plataforma."
        ),
    },
    "WEB_PERFORMANCE_RECORDED": "Rendimiento registrado",
}
