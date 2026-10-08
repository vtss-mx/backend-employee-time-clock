"""La cuenta: iniciar y cerrar sesión, sesiones, contraseña, dispositivos y ubicación de un validador, foto de perfil y
QR propio.

Textos en español de México (es-MX, el idioma por omisión): misma llave, mismos `{parámetros}` y mismas formas de plural
en los dos idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AUTH_BUSY": "Hay muchos inicios de sesión en este momento. Intenta de nuevo en unos segundos.",
    "AVATAR": "Foto de perfil",
    "AVATAR_CROP_INVALID": "El recorte necesita `crop_x`, `crop_y` y `crop_size`",
    "AVATAR_CROP_OUTSIDE": (
        "El recorte debe quedar dentro de la imagen ({width} × {height} px) y medir al menos {min} px"
    ),
    "AVATAR_DAMAGED": "La imagen está dañada o incompleta. Elige otra.",
    "AVATAR_EMPTY": "No se recibió ninguna imagen",
    "AVATAR_FORMAT": "La foto debe ser una imagen JPG, PNG o WEBP",
    "AVATAR_NOT_FOUND": "Esta persona no tiene foto de perfil",
    "AVATAR_REMOVED": "Foto de perfil eliminada",
    "AVATAR_SIZE_INVALID": "El tamaño de la foto debe ser 96 o 512",
    "AVATAR_TOO_LARGE": "La foto excede el tamaño máximo de {size}",
    "AVATAR_TOO_MANY_PIXELS": "La imagen supera los {max} megapíxeles",
    "AVATAR_TOO_SMALL": "La imagen es muy pequeña: cada lado debe medir al menos {min} px",
    "AVATAR_UPDATED": "Foto de perfil guardada",
    "COMPANY_INACTIVE": "Tu empresa está desactivada en la plataforma. Contacta al administrador.",
    "COMPANY_SELECTED": "Entraste a {company}",
    "COMPANY_SUSPENDED": "Tu empresa está suspendida. Contacta al administrador de la plataforma para reactivarla.",
    "CURRENT_PASSWORD_INVALID": "La contraseña actual no es correcta",
    "DEVICE_DESKTOP": "Computadora",
    "DEVICE_INVALID_TRANSITION": "Ese cambio no aplica al estado actual del dispositivo",
    "DEVICE_LOCATION_INACCURATE": (
        "La ubicación de tu dispositivo no es precisa (±{accuracy}). Activa la ubicación precisa o el GPS e intenta de "
        "nuevo."
    ),
    "DEVICE_NOT_FOUND": "Dispositivo no encontrado",
    "DEVICE_PENDING_APPROVAL": (
        "Este dispositivo quedó registrado como «{name}» y espera la autorización de tu empresa. Pide a un "
        "administrador que lo autorice en Validadores › Dispositivos."
    ),
    "DEVICE_PHONE": "Teléfono",
    "DEVICE_PROOF_INVALID": "No se pudo verificar este dispositivo. Inicia sesión de nuevo.",
    "DEVICE_PROOF_REQUIRED": (
        "Este validador solo funciona en dispositivos autorizados por la empresa: falta verificar este dispositivo"
    ),
    "DEVICE_REJECTED": "Tu empresa no autorizó este dispositivo («{name}»). Usa un dispositivo autorizado.",
    "DEVICE_REVOKED": (
        "Tu empresa retiró la autorización de este dispositivo («{name}»). Pide que lo autoricen de nuevo."
    ),
    "DEVICE_TABLET": "Tableta",
    "INVALID_CREDENTIALS": "Correo o contraseña incorrectos",
    "JWKS": "Llaves públicas de firma",
    "LOCATION_ACCURACY_MISSING": (
        "Tu dispositivo no indicó la precisión de tu ubicación. Activa la ubicación precisa o el GPS e intenta de "
        "nuevo."
    ),
    "LOCATION_OUT_OF_RANGE": (
        "Estás a {distance} del lugar de este validador. Acércate a menos de {radius} para iniciar sesión."
    ),
    "LOCATION_REQUIRED": (
        "Este validador solo inicia sesión en su lugar de operación. Permite el acceso a tu ubicación."
    ),
    "LOGGED_OUT": "Sesión cerrada",
    "LOGGED_OUT_ALL": {
        "one": "Se cerró {count} sesión",
        "other": "Se cerraron {count} sesiones",
    },
    "LOGIN_SUCCESS": "Sesión iniciada",
    "MY_QR": "Tu código QR",
    "MY_QR_STATUS": "Estado de tu código QR",
    "NOT_YOUR_COMPANY": "No trabajas en esa empresa",
    "NO_LONGER_IN_COMPANY": "Ya no tienes acceso a esta empresa. Inicia sesión de nuevo.",
    "PASSKEYS_LISTED": {
        "one": "{count} llave de acceso",
        "other": "{count} llaves de acceso",
    },
    "PASSKEY_ALREADY_REGISTERED": "Esa llave de acceso ya está registrada",
    "PASSKEY_CHALLENGE_INVALID": "El reto de la llave de acceso venció o no es válido. Intenta de nuevo.",
    "PASSKEY_CHALLENGE_USED": "Ese reto ya se usó. Intenta de nuevo.",
    "PASSKEY_CLONED": (
        "Esa llave de acceso se usó desde una copia y se revocó por seguridad. Entra con tu contraseña y registra una "
        "nueva."
    ),
    "PASSKEY_INVALID": "No se pudo verificar la llave de acceso que envió tu dispositivo. Intenta de nuevo.",
    "PASSKEY_LIMIT_REACHED": {
        "one": "Ya tienes {count} llave de acceso. Revoca una para registrar otra.",
        "other": "Ya tienes {count} llaves de acceso. Revoca una para registrar otra.",
    },
    "PASSKEY_LOGIN_FAILED": "No se pudo entrar con esa llave de acceso. Intenta de nuevo o usa tu contraseña.",
    "PASSKEY_LOGIN_OPTIONS": "Reto para entrar con una llave de acceso",
    "PASSKEY_NOT_FOUND": "Llave de acceso no encontrada",
    "PASSKEY_OPTIONS": "Reto para registrar una llave de acceso",
    "PASSKEY_REGISTERED": "Llave de acceso registrada",
    "PASSKEY_RENAMED": "Nombre de la llave guardado",
    "PASSKEY_REVOKED": "Llave de acceso revocada",
    "PASSWORD_CHANGED": {
        "zero": "Contraseña actualizada.",
        "one": "Contraseña actualizada. Se cerró {count} sesión en otros dispositivos.",
        "other": "Contraseña actualizada. Se cerraron {count} sesiones en otros dispositivos.",
    },
    "PASSWORD_REUSED": "La nueva contraseña debe ser distinta de la actual",
    "PREFERENCES_UPDATED": "Preferencias guardadas",
    "QR_DISABLED": "La verificación por QR está deshabilitada para tu empresa. Usa el reconocimiento facial.",
    "REMEMBERED_ACCOUNT": "Cuenta recordada",
    "REMEMBERED_ACCOUNT_FORGOTTEN": "Este dispositivo ya no recuerda la cuenta",
    "REMEMBERED_ACCOUNT_NONE": "Sin cuenta recordada",
    "SESSIONS_LISTED": {
        "one": "{count} sesión activa",
        "other": "{count} sesiones activas",
    },
    "SESSION_ACTIVE": "Sesión vigente",
    "SESSION_NONE": "Sin sesión",
    "SESSION_NOT_FOUND": "Sesión no encontrada",
    "SESSION_NO_LONGER_VALID": "Tu sesión ya no es válida. Inicia sesión de nuevo.",
    "SESSION_REVOKED": "Sesión revocada",
    "TOKEN_EXPIRED": "Tu sesión expiró. Inicia sesión de nuevo.",
    "TOKEN_INVALID": "Tu sesión no es válida. Inicia sesión de nuevo.",
    "TOKEN_REFRESHED": "Sesión renovada",
    "TOUCH_DEVICE_REQUIRED": (
        "Tu empresa solo permite validar identidades desde una tableta o un teléfono. Inicia sesión desde ese "
        "dispositivo con tu mismo correo y contraseña."
    ),
    "USER_INACTIVE": "La cuenta está desactivada",
    "USER_PROFILE": "Usuario autenticado",
    "YOUR_COMPANY": "tu empresa",
}
