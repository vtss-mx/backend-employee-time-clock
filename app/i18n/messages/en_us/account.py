"""La cuenta: iniciar y cerrar sesión, sesiones, contraseña, dispositivos y ubicación de un validador, foto de perfil y
QR propio.

Textos en inglés de Estados Unidos (en-US): misma llave, mismos `{parámetros}` y mismas formas de plural
en los dos idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AUTH_BUSY": "Too many sign-ins right now. Try again in a few seconds.",
    "AVATAR": "Profile photo",
    "AVATAR_CROP_INVALID": "The crop needs `crop_x`, `crop_y`, and `crop_size`",
    "AVATAR_CROP_OUTSIDE": "The crop must be inside the image ({width} × {height} px) and at least {min} px",
    "AVATAR_DAMAGED": "The image is damaged or incomplete. Choose another one.",
    "AVATAR_EMPTY": "No image was received",
    "AVATAR_FORMAT": "The photo must be a JPG, PNG, or WEBP image",
    "AVATAR_NOT_FOUND": "This person doesn't have a profile photo",
    "AVATAR_REMOVED": "Profile photo removed",
    "AVATAR_SIZE_INVALID": "The photo size must be 96 or 512",
    "AVATAR_TOO_LARGE": "The photo is larger than the maximum size of {size}",
    "AVATAR_TOO_MANY_PIXELS": "The image exceeds {max} megapixels",
    "AVATAR_TOO_SMALL": "The image is too small: each side must be at least {min} px",
    "AVATAR_UPDATED": "Profile photo saved",
    "COMPANY_INACTIVE": "Your company is deactivated on the platform. Contact the administrator.",
    "COMPANY_SELECTED": "Switched to {company}",
    "COMPANY_SUSPENDED": "Your company is suspended. Contact the platform administrator to reactivate it.",
    "CURRENT_PASSWORD_INVALID": "The current password isn't correct",
    "DEVICE_DESKTOP": "Computer",
    "DEVICE_INVALID_TRANSITION": "That change doesn't apply to the device's current status",
    "DEVICE_LOCATION_INACCURATE": (
        "Your device's location isn't precise (±{accuracy}). Turn on precise location or GPS and try again."
    ),
    "DEVICE_NOT_FOUND": "Device not found",
    "DEVICE_PENDING_APPROVAL": (
        "This device was registered as “{name}” and is waiting for your company's approval. Ask an administrator to "
        "approve it in Validators › Devices."
    ),
    "DEVICE_PHONE": "Phone",
    "DEVICE_PROOF_INVALID": "Couldn't verify this device. Sign in again.",
    "DEVICE_PROOF_REQUIRED": (
        "This validator only works on devices approved by the company: this device still needs to be verified"
    ),
    "DEVICE_REJECTED": "Your company didn't approve this device (“{name}”). Use an approved device.",
    "DEVICE_REVOKED": "Your company withdrew its approval of this device (“{name}”). Ask them to approve it again.",
    "DEVICE_TABLET": "Tablet",
    "INVALID_CREDENTIALS": "Incorrect email or password",
    "JWKS": "Public signing keys",
    "LOCATION_ACCURACY_MISSING": (
        "Your device didn't report its location accuracy. Turn on precise location or GPS and try again."
    ),
    "LOCATION_OUT_OF_RANGE": "You're {distance} from this validator's location. Move within {radius} to sign in.",
    "LOCATION_REQUIRED": "This validator can only sign in at its location. Allow access to your location.",
    "LOGGED_OUT": "Signed out",
    "LOGGED_OUT_ALL": {
        "one": "{count} session was closed",
        "other": "{count} sessions were closed",
    },
    "LOGIN_SUCCESS": "Signed in",
    "MY_QR": "Your QR code",
    "MY_QR_STATUS": "Your QR code status",
    "NOT_YOUR_COMPANY": "You don't work at that company",
    "NO_LONGER_IN_COMPANY": "You no longer have access to this company. Sign in again.",
    "PASSWORD_CHANGED": {
        "zero": "Password updated.",
        "one": "Password updated. {count} session on other devices was closed.",
        "other": "Password updated. {count} sessions on other devices were closed.",
    },
    "PASSWORD_REUSED": "The new password must be different from the current one",
    "PREFERENCES_UPDATED": "Preferences saved",
    "QR_DISABLED": "QR verification is turned off for your company. Use face recognition.",
    "REMEMBERED_ACCOUNT": "Account remembered",
    "REMEMBERED_ACCOUNT_FORGOTTEN": "This device no longer remembers the account",
    "REMEMBERED_ACCOUNT_NONE": "No remembered account",
    "SESSIONS_LISTED": {
        "one": "{count} active session",
        "other": "{count} active sessions",
    },
    "SESSION_ACTIVE": "Active session",
    "SESSION_NONE": "No session",
    "SESSION_NOT_FOUND": "Session not found",
    "SESSION_NO_LONGER_VALID": "Your session is no longer valid. Sign in again.",
    "SESSION_REVOKED": "Session revoked",
    "TOKEN_EXPIRED": "Your session expired. Sign in again.",
    "TOKEN_INVALID": "Your session isn't valid. Sign in again.",
    "TOKEN_REFRESHED": "Session renewed",
    "TOUCH_DEVICE_REQUIRED": (
        "Your company only allows identity validation from a tablet or phone. Sign in from that device with the same "
        "email and password."
    ),
    "USER_INACTIVE": "The account is deactivated",
    "USER_PROFILE": "Authenticated user",
    "YOUR_COMPANY": "your company",
}
