"""Identidad: registro facial, identificación, punto de control del validador y QR.

Textos en italiano (it-IT), traducidos del sentido de es-MX: misma llave, mismos `{parámetros}` y mismas formas de
plural en todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ANSWER_ACCEPTED": "Risposta accettata",
    "ANSWER_ALREADY_ACCEPTED": "Questa domanda ha già ricevuto risposta",
    "ANSWER_INAUDIBLE": "La tua voce non si è sentita. Parla più forte e più vicino al microfono.",
    "ANSWER_MISMATCH": "La risposta non corrisponde ai tuoi dati registrati. Rispondi di nuovo.",
    "ANSWER_TOO_LONG": "La risposta è troppo lunga. Rispondi in meno di {seconds} secondi.",
    "ANSWER_TOO_SHORT": "La risposta è troppo breve. Pronuncia la risposta completa.",
    "ANSWER_UNCLEAR": "La tua risposta non è stata compresa. Parla chiaramente e lentamente.",
    "CHALLENGE_ISSUED": "Sfida della verifica di vivacità emessa",
    "CHECKPOINT_LOCATION_OUT_OF_RANGE": (
        "Sei a {distance} dal luogo di questo validatore. Avvicinati a meno di {radius} per identificare."
    ),
    "CHECKPOINT_LOCATION_REQUIRED": (
        "Questo validatore identifica solo nel suo luogo di operatività. Consenti l'accesso alla tua posizione."
    ),
    "CHECKPOINT_PROFILE": "Validatore",
    "CHECKPOINT_RECENT": "Identificazioni recenti",
    "EMPLOYEE_FACE_NOT_APPROVED": "Il dipendente non ha ancora un volto approvato: registralo prima",
    "EMPLOYEE_INACTIVE_ENROLL": "Il dipendente è inattivo: attivalo prima di registrarne il volto",
    "ENROLLMENTS_LISTED": {
        "one": "{count} registrazione del volto",
        "other": "{count} registrazioni del volto",
    },
    "ENROLLMENT_ALREADY_APPROVED": "La registrazione del tuo volto è già stata approvata",
    "ENROLLMENT_ALREADY_REVIEWED": "Questa registrazione è già stata revisionata",
    "ENROLLMENT_APPROVED_DONE": "Utente accettato. Ora può verificare la sua identità.",
    "ENROLLMENT_EMPLOYEE_INACTIVE": "Solo i dipendenti attivi possono registrare il volto",
    "ENROLLMENT_FOUND": "Registrazione del volto trovata",
    "ENROLLMENT_NOT_FOUND": "Registrazione del volto non trovata",
    "ENROLLMENT_PENDING": "La registrazione del tuo volto è già stata inviata ed è in attesa di convalida",
    "ENROLLMENT_PHOTOS_ACCEPTED": "Foto accettate. Ora rispondi alle domande in video.",
    "ENROLLMENT_PHOTO_EXPIRED": "La tua prima foto è scaduta. Scattala di nuovo.",
    "ENROLLMENT_PHOTO_MISMATCH": (
        "Le acquisizioni non corrispondono alla tua prima foto. Ripeti le acquisizioni con il tuo volto."
    ),
    "ENROLLMENT_PHOTO_REQUIRED": "Inizia con la tua prima foto",
    "ENROLLMENT_PHOTO_SAVED": "Prima foto salvata. Ora prosegui con le acquisizioni.",
    "ENROLLMENT_PROGRESS": "Avanzamento della registrazione del volto",
    "ENROLLMENT_REJECTED": "Utente rifiutato. Dovrà registrare di nuovo il volto.",
    "ENROLLMENT_SENT": "La registrazione del tuo volto è stata inviata ed è in attesa di convalida",
    "ENROLLMENT_SUBMITTED": "Registrazione del volto inviata. La tua identità è in attesa di convalida.",
    "ENROLLMENT_VOICE_DONE": "Verifica vocale completata. La tua identità è in attesa di convalida.",
    "FACE_ALREADY_REGISTERED_AS": "{message} ({name}, {number})",
    "FACE_ALREADY_REGISTERED_AS_NAME": "{message} ({name})",
    "FACE_CHECK_PASSED": "L'acquisizione è valida",
    "FACE_ENROLLED_IN_PERSON": "Volto registrato e approvato: il dipendente ora può identificarsi",
    "FACE_SIGNAL_ANTISPOOF_REAL": "Probabilità minima di volto reale",
    "FACE_SIGNAL_BURST_MOTION": "Movimento naturale minimo della raffica",
    "FACE_SIGNAL_FLASH_RATIO": "Rapporto minimo volto/sfondo del lampo",
    "FACE_SIGNAL_FLASH_SCORE": "Risposta minima al lampo di colori",
    "FACE_SIGNAL_LIVENESS_CLOSER": "Avvicinamento minimo alla fotocamera",
    "FACE_SIGNAL_LIVENESS_PITCH": "Movimento minimo guardando in alto o in basso",
    "FACE_SIGNAL_LIVENESS_YAW": "Rotazione minima della testa",
    "FACE_SIGNAL_MOIRE": "Trama dello schermo massima",
    "FACE_SIGNAL_NOISE_RATIO": "Rapporto minimo di rumore volto/sfondo",
    "FACE_SIGNAL_PARALLAX": "Parallasse minima ruotando la testa",
    "FLASH_COLORS": "Colori del lampo",
    "FLASH_TOKEN_INVALID": "Il lampo di questa sfida è scaduto o non è valido. Richiedi un'altra sfida.",
    "IDENTIFICATION_SUCCESS": "Identità confermata",
    "IMAGE_REQUIRED": "Invia almeno un'acquisizione",
    "IMAGE_VALID": "Immagine valida",
    "INVALID_FRAME_COUNT": "Invia da {min} a {max} acquisizioni frontali",
    "LIVENESS_NOT_REQUIRED": "Verifica di vivacità non richiesta",
    "PHOTO_ERROR": "Foto {number}: {message}",
    "QR_FACE_MISMATCH": "Il volto non corrisponde al titolare del codice QR",
    "QR_HOLDER_FOUND": "Ora verifica il volto di {name}",
    "QR_NOT_FOUND": "Codice QR non trovato",
    "QR_REQUIRED": "Scansiona prima il codice QR del dipendente",
    "QR_WITHOUT_ATTENDANCE": (
        "Identificazione con QR effettuata. Per registrare la presenza, identificati con il tuo volto."
    ),
    "REJECTION_REASON_REQUIRED": "Indica il motivo del rifiuto",
    "SIGNATURE_INVALID": "La firma di questo dispositivo non è valida. Accedi di nuovo.",
    "SIGNATURE_KEY_MISMATCH": (
        "Questo non è il dispositivo con cui hai effettuato l'accesso. Accedi di nuovo da questo dispositivo."
    ),
    "SIGNATURE_REQUIRED": (
        "Questo validatore deve firmare ogni identificazione con il suo dispositivo. "
        "Usa l'applicazione su un dispositivo autorizzato."
    ),
    "SIGNATURE_STALE": "La firma di questo dispositivo è scaduta. Riprova.",
    "SPEECH_SERVICE_UNAVAILABLE": "Il servizio vocale non è disponibile. Riprova tra qualche minuto.",
    "VALIDATOR_METHOD_NOT_ALLOWED": "Questo validatore identifica in modalità «{mode}»",
    "VALIDATOR_REQUIRED": "Questo account non è un validatore di identità",
    "VERIFICATION_LOCATION_INVALID": (
        "La tua posizione non è valida o non è abbastanza precisa. Attiva la posizione precisa (GPS) e riprova."
    ),
    "VERIFICATION_LOCATION_REQUIRED": (
        "È necessaria la tua posizione per verificare la tua identità. Consenti l'accesso alla posizione e riprova."
    ),
    "VIDEO_FACE_MISMATCH": (
        "Il volto del video non corrisponde alle tue foto. Tieni il volto davanti alla fotocamera e rispondi di nuovo."
    ),
    "VIDEO_TOO_LARGE": "Il video è troppo grande (massimo {size})",
    "VIDEO_UNSUPPORTED_FORMAT": "Impossibile leggere il video. Usa Chrome, Safari, Edge o Firefox aggiornati.",
    "VOICE_CLIP_FOUND": "Video della risposta",
    "VOICE_CLIP_NOT_FOUND": "Video non trovato",
    "VOICE_NOT_PENDING": "Questa registrazione non ha una verifica vocale in sospeso",
    "VOICE_RETRIES_EXHAUSTED": (
        "I tentativi della verifica vocale sono esauriti. Ripeti la prima foto e le acquisizioni."
    ),
    "VOICE_SESSION_EXPIRED": "La verifica vocale è scaduta. Riaprila: le tue risposte accettate vengono conservate.",
    "VOICE_SESSION_INVALID": (
        "La verifica vocale non è valida. Riaprila: le tue risposte accettate vengono conservate."
    ),
    "VOICE_SESSION_STARTED": "Domande in video pronte",
    "VOICE_SUM_PROMPT": "Quanto fa {a} più {b}?",
}
