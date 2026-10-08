"""La cuenta: iniciar y cerrar sesión, sesiones, contraseña, dispositivos y ubicación de un validador, foto de perfil y
QR propio.

Textos en alemán de Alemania (de-DE, trato de «Sie»): misma llave, mismos `{parámetros}` y mismas formas de plural
en todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AUTH_BUSY": "Gerade melden sich sehr viele Personen an. Versuchen Sie es in einigen Sekunden erneut.",
    "AVATAR": "Profilfoto",
    "AVATAR_CROP_INVALID": "Für den Zuschnitt sind `crop_x`, `crop_y` und `crop_size` erforderlich",
    "AVATAR_CROP_OUTSIDE": (
        "Der Zuschnitt muss innerhalb des Bildes liegen ({width} × {height} px) und mindestens {min} px groß sein"
    ),
    "AVATAR_DAMAGED": "Das Bild ist beschädigt oder unvollständig. Wählen Sie ein anderes.",
    "AVATAR_EMPTY": "Es wurde kein Bild empfangen",
    "AVATAR_FORMAT": "Das Foto muss ein Bild im Format JPG, PNG oder WEBP sein",
    "AVATAR_NOT_FOUND": "Diese Person hat kein Profilfoto",
    "AVATAR_REMOVED": "Profilfoto entfernt",
    "AVATAR_SIZE_INVALID": "Die Fotogröße muss 96 oder 512 sein",
    "AVATAR_TOO_LARGE": "Das Foto überschreitet die maximale Größe von {size}",
    "AVATAR_TOO_MANY_PIXELS": "Das Bild hat mehr als {max} Megapixel",
    "AVATAR_TOO_SMALL": "Das Bild ist zu klein: Jede Seite muss mindestens {min} px messen",
    "AVATAR_UPDATED": "Profilfoto gespeichert",
    "COMPANY_INACTIVE": "Ihr Unternehmen ist auf der Plattform deaktiviert. Wenden Sie sich an den Administrator.",
    "COMPANY_SELECTED": "Aktuelles Unternehmen: {company}",
    "COMPANY_SUSPENDED": (
        "Ihr Unternehmen ist gesperrt. Wenden Sie sich an den Administrator der Plattform, um es wieder zu aktivieren."
    ),
    "CURRENT_PASSWORD_INVALID": "Das aktuelle Passwort ist falsch",
    "DEVICE_DESKTOP": "Computer",
    "DEVICE_INVALID_TRANSITION": "Diese Änderung ist im aktuellen Status des Geräts nicht möglich",
    "DEVICE_LOCATION_INACCURATE": (
        "Der Standort Ihres Geräts ist ungenau (±{accuracy}). Aktivieren Sie die genaue Standortbestimmung oder GPS "
        "und versuchen Sie es erneut."
    ),
    "DEVICE_NOT_FOUND": "Gerät nicht gefunden",
    "DEVICE_PENDING_APPROVAL": (
        "Dieses Gerät wurde als „{name}“ registriert und wartet auf die Freigabe durch Ihr Unternehmen. Bitten Sie "
        "einen Administrator, es unter Prüfgeräte › Geräte freizugeben."
    ),
    "DEVICE_PHONE": "Telefon",
    "DEVICE_PROOF_INVALID": "Dieses Gerät konnte nicht verifiziert werden. Melden Sie sich erneut an.",
    "DEVICE_PROOF_REQUIRED": (
        "Dieses Prüfgerät funktioniert nur auf Geräten, die das Unternehmen freigegeben hat: Dieses Gerät muss noch "
        "verifiziert werden"
    ),
    "DEVICE_REJECTED": (
        "Ihr Unternehmen hat dieses Gerät („{name}“) nicht freigegeben. Verwenden Sie ein freigegebenes Gerät."
    ),
    "DEVICE_REVOKED": (
        "Ihr Unternehmen hat die Freigabe dieses Geräts („{name}“) zurückgezogen. Bitten Sie um eine erneute Freigabe."
    ),
    "DEVICE_TABLET": "Tabletcomputer",
    "INVALID_CREDENTIALS": "E-Mail-Adresse oder Passwort falsch",
    "JWKS": "Öffentliche Schlüssel für Signaturen",
    "LOCATION_ACCURACY_MISSING": (
        "Ihr Gerät hat die Genauigkeit Ihres Standorts nicht übermittelt. Aktivieren Sie die genaue "
        "Standortbestimmung oder GPS und versuchen Sie es erneut."
    ),
    "LOCATION_OUT_OF_RANGE": (
        "Sie sind {distance} vom Einsatzort dieses Prüfgeräts entfernt. Kommen Sie näher als {radius}, um sich "
        "anzumelden."
    ),
    "LOCATION_REQUIRED": (
        "Dieses Prüfgerät kann sich nur an seinem Einsatzort anmelden. Erlauben Sie den Zugriff auf Ihren Standort."
    ),
    "LOGGED_OUT": "Abgemeldet",
    "LOGGED_OUT_ALL": {
        "one": "{count} Sitzung beendet",
        "other": "{count} Sitzungen beendet",
    },
    "LOGIN_SUCCESS": "Angemeldet",
    "MY_QR": "Ihr QR-Code",
    "MY_QR_STATUS": "Status Ihres QR-Codes",
    "NOT_YOUR_COMPANY": "Sie arbeiten nicht bei diesem Unternehmen",
    "NO_LONGER_IN_COMPANY": "Sie haben keinen Zugriff mehr auf dieses Unternehmen. Melden Sie sich erneut an.",
    "PASSKEYS_LISTED": {
        "one": "{count} Zugangsschlüssel",
        "other": "{count} Zugangsschlüssel",
    },
    "PASSKEY_ALREADY_REGISTERED": "Dieser Zugangsschlüssel ist bereits registriert",
    "PASSKEY_CHALLENGE_INVALID": (
        "Die Aufgabe des Zugangsschlüssels ist abgelaufen oder ungültig. Versuchen Sie es erneut."
    ),
    "PASSKEY_CHALLENGE_USED": "Diese Aufgabe wurde bereits verwendet. Versuchen Sie es erneut.",
    "PASSKEY_CLONED": (
        "Dieser Zugangsschlüssel wurde von einer Kopie aus verwendet und zur Sicherheit widerrufen. Melden Sie sich "
        "mit Ihrem Passwort an und registrieren Sie einen neuen."
    ),
    "PASSKEY_INVALID": (
        "Der von Ihrem Gerät gesendete Zugangsschlüssel konnte nicht geprüft werden. Versuchen Sie es erneut."
    ),
    "PASSKEY_LIMIT_REACHED": {
        "one": "Sie haben bereits {count} Zugangsschlüssel. Widerrufen Sie einen, um einen weiteren zu registrieren.",
        "other": "Sie haben bereits {count} Zugangsschlüssel. Widerrufen Sie einen, um einen weiteren zu registrieren.",
    },
    "PASSKEY_LOGIN_FAILED": (
        "Die Anmeldung mit diesem Zugangsschlüssel ist fehlgeschlagen. Versuchen Sie es erneut oder verwenden Sie Ihr "
        "Passwort."
    ),
    "PASSKEY_LOGIN_OPTIONS": "Aufgabe für die Anmeldung mit einem Zugangsschlüssel",
    "PASSKEY_NOT_FOUND": "Zugangsschlüssel nicht gefunden",
    "PASSKEY_OPTIONS": "Aufgabe für die Registrierung eines Zugangsschlüssels",
    "PASSKEY_REGISTERED": "Zugangsschlüssel registriert",
    "PASSKEY_RENAMED": "Name des Schlüssels gespeichert",
    "PASSKEY_REVOKED": "Zugangsschlüssel widerrufen",
    "PASSWORD_CHANGED": {
        "zero": "Passwort aktualisiert.",
        "one": "Passwort aktualisiert. {count} Sitzung auf anderen Geräten wurde beendet.",
        "other": "Passwort aktualisiert. {count} Sitzungen auf anderen Geräten wurden beendet.",
    },
    "PASSWORD_REUSED": "Das neue Passwort muss sich vom aktuellen unterscheiden",
    "PREFERENCES_UPDATED": "Einstellungen gespeichert",
    "QR_DISABLED": (
        "Die Prüfung per QR-Code ist für Ihr Unternehmen deaktiviert. Verwenden Sie die Gesichtserkennung."
    ),
    "REMEMBERED_ACCOUNT": "Gespeichertes Konto",
    "REMEMBERED_ACCOUNT_FORGOTTEN": "Dieses Gerät merkt sich das Konto nicht mehr",
    "REMEMBERED_ACCOUNT_NONE": "Kein gespeichertes Konto",
    "SESSIONS_LISTED": {
        "one": "{count} aktive Sitzung",
        "other": "{count} aktive Sitzungen",
    },
    "SESSION_ACTIVE": "Aktive Sitzung",
    "SESSION_NONE": "Keine Sitzung",
    "SESSION_NOT_FOUND": "Sitzung nicht gefunden",
    "SESSION_NO_LONGER_VALID": "Ihre Sitzung ist nicht mehr gültig. Melden Sie sich erneut an.",
    "SESSION_REVOKED": "Sitzung widerrufen",
    "TOKEN_EXPIRED": "Ihre Sitzung ist abgelaufen. Melden Sie sich erneut an.",
    "TOKEN_INVALID": "Ihre Sitzung ist ungültig. Melden Sie sich erneut an.",
    "TOKEN_REFRESHED": "Sitzung verlängert",
    "TOUCH_DEVICE_REQUIRED": (
        "Ihr Unternehmen erlaubt die Identitätsprüfung nur auf einem Tabletcomputer oder Telefon. Melden Sie sich auf "
        "diesem Gerät mit derselben E-Mail-Adresse und demselben Passwort an."
    ),
    "USER_INACTIVE": "Das Konto ist deaktiviert",
    "USER_PROFILE": "Angemeldeter Benutzer",
    "YOUR_COMPANY": "Ihr Unternehmen",
}
