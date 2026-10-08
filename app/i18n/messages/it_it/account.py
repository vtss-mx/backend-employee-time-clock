"""La cuenta: iniciar y cerrar sesión, sesiones, contraseña, dispositivos y ubicación de un validador, foto de perfil y
QR propio.

Textos en italiano (it-IT), traducidos del sentido de es-MX: misma llave, mismos `{parámetros}` y mismas formas de
plural en todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AUTH_BUSY": "Troppi accessi in questo momento. Riprova tra qualche secondo.",
    "AVATAR": "Foto del profilo",
    "AVATAR_CROP_INVALID": "Il ritaglio richiede `crop_x`, `crop_y` e `crop_size`",
    "AVATAR_CROP_OUTSIDE": (
        "Il ritaglio deve restare all'interno dell'immagine ({width} × {height} px) e misurare almeno {min} px"
    ),
    "AVATAR_DAMAGED": "L'immagine è danneggiata o incompleta. Scegline un'altra.",
    "AVATAR_EMPTY": "Nessuna immagine ricevuta",
    "AVATAR_FORMAT": "La foto deve essere un'immagine JPG, PNG o WEBP",
    "AVATAR_NOT_FOUND": "Questa persona non ha una foto del profilo",
    "AVATAR_REMOVED": "Foto del profilo eliminata",
    "AVATAR_SIZE_INVALID": "La dimensione della foto deve essere 96 o 512",
    "AVATAR_TOO_LARGE": "La foto supera la dimensione massima di {size}",
    "AVATAR_TOO_MANY_PIXELS": "L'immagine supera i {max} milioni di pixel",
    "AVATAR_TOO_SMALL": "L'immagine è troppo piccola: ogni lato deve misurare almeno {min} px",
    "AVATAR_UPDATED": "Foto del profilo salvata",
    "COMPANY_INACTIVE": "La tua azienda è disattivata sulla piattaforma. Contatta l'amministratore.",
    "COMPANY_SELECTED": "Ora operi in {company}",
    "COMPANY_SUSPENDED": "La tua azienda è sospesa. Contatta l'amministratore della piattaforma per riattivarla.",
    "CURRENT_PASSWORD_INVALID": "La password attuale non è corretta",
    "DEVICE_DESKTOP": "Computer",
    "DEVICE_INVALID_TRANSITION": "Questa modifica non si applica allo stato attuale del dispositivo",
    "DEVICE_LOCATION_INACCURATE": (
        "La posizione del tuo dispositivo non è precisa (±{accuracy}). Attiva la posizione precisa o il GPS e riprova."
    ),
    "DEVICE_NOT_FOUND": "Dispositivo non trovato",
    "DEVICE_PENDING_APPROVAL": (
        "Questo dispositivo è stato registrato come «{name}» ed è in attesa dell'autorizzazione della tua azienda. "
        "Chiedi a un amministratore di autorizzarlo in Validatori › Dispositivi."
    ),
    "DEVICE_PHONE": "Telefono",
    "DEVICE_PROOF_INVALID": "Impossibile verificare questo dispositivo. Accedi di nuovo.",
    "DEVICE_PROOF_REQUIRED": (
        "Questo validatore funziona solo su dispositivi autorizzati dall'azienda: questo dispositivo deve ancora "
        "essere verificato"
    ),
    "DEVICE_REJECTED": (
        "La tua azienda non ha autorizzato questo dispositivo («{name}»). Usa un dispositivo autorizzato."
    ),
    "DEVICE_REVOKED": (
        "La tua azienda ha revocato l'autorizzazione di questo dispositivo («{name}»). Chiedi che venga autorizzato di "
        "nuovo."
    ),
    "DEVICE_TABLET": "Tablet",
    "INVALID_CREDENTIALS": "E-mail o password non corrette",
    "JWKS": "Chiavi pubbliche di firma",
    "LOCATION_ACCURACY_MISSING": (
        "Il tuo dispositivo non ha indicato la precisione della posizione. Attiva la posizione precisa o il GPS e "
        "riprova."
    ),
    "LOCATION_OUT_OF_RANGE": (
        "Sei a {distance} dal luogo di questo validatore. Avvicinati a meno di {radius} per accedere."
    ),
    "LOCATION_REQUIRED": (
        "Questo validatore può accedere solo nel suo luogo di operatività. Consenti l'accesso alla tua posizione."
    ),
    "LOGGED_OUT": "Sessione chiusa",
    "LOGGED_OUT_ALL": {
        "one": "{count} sessione chiusa",
        "other": "{count} sessioni chiuse",
    },
    "LOGIN_SUCCESS": "Accesso effettuato",
    "MY_QR": "Il tuo codice QR",
    "MY_QR_STATUS": "Stato del tuo codice QR",
    "NOT_YOUR_COMPANY": "Non lavori in questa azienda",
    "NO_LONGER_IN_COMPANY": "Non hai più accesso a questa azienda. Accedi di nuovo.",
    "PASSKEYS_LISTED": {
        "one": "{count} chiave di accesso",
        "other": "{count} chiavi di accesso",
    },
    "PASSKEY_ALREADY_REGISTERED": "Quella chiave di accesso è già registrata",
    "PASSKEY_CHALLENGE_INVALID": "La sfida della chiave di accesso è scaduta o non è valida. Riprova.",
    "PASSKEY_CHALLENGE_USED": "Quella sfida è già stata usata. Riprova.",
    "PASSKEY_CLONED": (
        "Quella chiave di accesso è stata usata da una copia ed è stata revocata per sicurezza. Accedi con la tua "
        "password e registrane una nuova."
    ),
    "PASSKEY_INVALID": "Impossibile verificare la chiave di accesso inviata dal tuo dispositivo. Riprova.",
    "PASSKEY_LIMIT_REACHED": {
        "one": "Hai già {count} chiave di accesso. Revocane una per registrarne un'altra.",
        "other": "Hai già {count} chiavi di accesso. Revocane una per registrarne un'altra.",
    },
    "PASSKEY_LOGIN_FAILED": "Impossibile accedere con quella chiave di accesso. Riprova o usa la tua password.",
    "PASSKEY_LOGIN_OPTIONS": "Sfida per accedere con una chiave di accesso",
    "PASSKEY_NOT_FOUND": "Chiave di accesso non trovata",
    "PASSKEY_OPTIONS": "Sfida per registrare una chiave di accesso",
    "PASSKEY_REGISTERED": "Chiave di accesso registrata",
    "PASSKEY_RENAMED": "Nome della chiave salvato",
    "PASSKEY_REVOKED": "Chiave di accesso revocata",
    "PASSWORD_CHANGED": {
        "zero": "Password aggiornata.",
        "one": "Password aggiornata. {count} sessione chiusa su altri dispositivi.",
        "other": "Password aggiornata. {count} sessioni chiuse su altri dispositivi.",
    },
    "PASSWORD_REUSED": "La nuova password deve essere diversa da quella attuale",
    "PREFERENCES_UPDATED": "Preferenze salvate",
    "QR_DISABLED": "La verifica tramite QR è disattivata per la tua azienda. Usa il riconoscimento facciale.",
    "REMEMBERED_ACCOUNT": "Account memorizzato",
    "REMEMBERED_ACCOUNT_FORGOTTEN": "Questo dispositivo non ricorda più l'account",
    "REMEMBERED_ACCOUNT_NONE": "Nessun account memorizzato",
    "SESSIONS_LISTED": {
        "one": "{count} sessione attiva",
        "other": "{count} sessioni attive",
    },
    "SESSION_ACTIVE": "Sessione valida",
    "SESSION_NONE": "Nessuna sessione",
    "SESSION_NOT_FOUND": "Sessione non trovata",
    "SESSION_NO_LONGER_VALID": "La tua sessione non è più valida. Accedi di nuovo.",
    "SESSION_REVOKED": "Sessione revocata",
    "TOKEN_EXPIRED": "La tua sessione è scaduta. Accedi di nuovo.",
    "TOKEN_INVALID": "La tua sessione non è valida. Accedi di nuovo.",
    "TOKEN_REFRESHED": "Sessione rinnovata",
    "TOUCH_DEVICE_REQUIRED": (
        "La tua azienda consente di verificare le identità solo da un tablet o da un telefono. Accedi da quel "
        "dispositivo con la stessa e-mail e la stessa password."
    ),
    "USER_INACTIVE": "L'account è disattivato",
    "USER_PROFILE": "Utente autenticato",
    "YOUR_COMPANY": "la tua azienda",
}
