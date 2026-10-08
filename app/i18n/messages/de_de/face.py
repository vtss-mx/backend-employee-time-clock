"""Identidad: registro facial, identificación, punto de control del validador y QR.

Textos en alemán de Alemania (de-DE, trato de «Sie»): misma llave, mismos `{parámetros}` y mismas formas de plural
en todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ANSWER_ACCEPTED": "Antwort angenommen",
    "ANSWER_ALREADY_ACCEPTED": "Diese Frage wurde bereits beantwortet",
    "ANSWER_INAUDIBLE": "Ihre Stimme war nicht zu hören. Sprechen Sie lauter und näher am Mikrofon.",
    "ANSWER_MISMATCH": "Die Antwort stimmt nicht mit Ihren registrierten Daten überein. Antworten Sie erneut.",
    "ANSWER_TOO_LONG": "Die Antwort ist zu lang. Antworten Sie in weniger als {seconds} Sekunden.",
    "ANSWER_TOO_SHORT": "Die Antwort ist zu kurz. Sprechen Sie Ihre vollständige Antwort.",
    "ANSWER_UNCLEAR": "Ihre Antwort wurde nicht verstanden. Sprechen Sie deutlich und langsam.",
    "CHALLENGE_ISSUED": "Aufgabe zur Lebenderkennung erstellt",
    "CHECKPOINT_LOCATION_OUT_OF_RANGE": (
        "Sie sind {distance} vom Einsatzort dieses Prüfgeräts entfernt. Kommen Sie näher als {radius}, um Personen "
        "zu identifizieren."
    ),
    "CHECKPOINT_LOCATION_REQUIRED": (
        "Dieses Prüfgerät identifiziert nur an seinem Einsatzort. Erlauben Sie den Zugriff auf Ihren Standort."
    ),
    "CHECKPOINT_PROFILE": "Prüfgerät",
    "CHECKPOINT_RECENT": "Letzte Identifizierungen",
    "EMPLOYEE_FACE_NOT_APPROVED": "Der Mitarbeiter hat noch kein freigegebenes Gesicht: Registrieren Sie es zuerst",
    "EMPLOYEE_INACTIVE_ENROLL": (
        "Der Mitarbeiter ist inaktiv: Aktivieren Sie ihn, bevor Sie sein Gesicht registrieren"
    ),
    "ENROLLMENTS_LISTED": {
        "one": "{count} Gesichtsregistrierung",
        "other": "{count} Gesichtsregistrierungen",
    },
    "ENROLLMENT_ALREADY_APPROVED": "Ihre Gesichtsregistrierung wurde bereits freigegeben",
    "ENROLLMENT_ALREADY_REVIEWED": "Diese Registrierung wurde bereits geprüft",
    "ENROLLMENT_APPROVED_DONE": "Benutzer freigegeben. Die Person kann ihre Identität jetzt bestätigen.",
    "ENROLLMENT_EMPLOYEE_INACTIVE": "Nur aktive Mitarbeiter können ihr Gesicht registrieren",
    "ENROLLMENT_FOUND": "Gesichtsregistrierung gefunden",
    "ENROLLMENT_NOT_FOUND": "Gesichtsregistrierung nicht gefunden",
    "ENROLLMENT_PENDING": "Ihre Gesichtsregistrierung wurde bereits gesendet und wird geprüft",
    "ENROLLMENT_PHOTOS_ACCEPTED": "Fotos angenommen. Beantworten Sie jetzt die Fragen im Video.",
    "ENROLLMENT_PHOTO_EXPIRED": "Ihr erstes Foto ist abgelaufen. Nehmen Sie es erneut auf.",
    "ENROLLMENT_PHOTO_MISMATCH": (
        "Die Aufnahmen stimmen nicht mit Ihrem ersten Foto überein. Wiederholen Sie die Aufnahmen mit Ihrem Gesicht."
    ),
    "ENROLLMENT_PHOTO_REQUIRED": "Nehmen Sie zuerst Ihr erstes Foto auf",
    "ENROLLMENT_PHOTO_SAVED": "Erstes Foto gespeichert. Fahren Sie jetzt mit den Aufnahmen fort.",
    "ENROLLMENT_PROGRESS": "Fortschritt der Gesichtsregistrierung",
    "ENROLLMENT_REJECTED": "Benutzer abgelehnt. Die Person muss ihr Gesicht erneut registrieren.",
    "ENROLLMENT_SENT": "Ihre Gesichtsregistrierung wurde gesendet und wird geprüft",
    "ENROLLMENT_SUBMITTED": "Gesichtsregistrierung gesendet. Ihre Identität wird geprüft.",
    "ENROLLMENT_VOICE_DONE": "Stimmprüfung abgeschlossen. Ihre Identität wird geprüft.",
    "FACE_ALREADY_REGISTERED_AS": "{message} ({name}, {number})",
    "FACE_ALREADY_REGISTERED_AS_NAME": "{message} ({name})",
    "FACE_CHECK_PASSED": "Die Aufnahme ist gültig",
    "FACE_ENROLLED_IN_PERSON": "Gesicht registriert und freigegeben: Der Mitarbeiter kann sich jetzt identifizieren",
    "FACE_SIGNAL_ANTISPOOF_REAL": "Mindestwahrscheinlichkeit für ein echtes Gesicht",
    "FACE_SIGNAL_BURST_MOTION": "Minimale natürliche Bewegung in der Aufnahmeserie",
    "FACE_SIGNAL_FLASH_RATIO": "Minimales Verhältnis Gesicht zu Hintergrund beim Blitz",
    "FACE_SIGNAL_FLASH_SCORE": "Minimale Reaktion auf den Farbblitz",
    "FACE_SIGNAL_LIVENESS_CLOSER": "Minimale Annäherung an die Kamera",
    "FACE_SIGNAL_LIVENESS_PITCH": "Minimale Bewegung beim Blick nach oben oder unten",
    "FACE_SIGNAL_LIVENESS_YAW": "Minimale Kopfdrehung",
    "FACE_SIGNAL_MOIRE": "Maximales Bildschirmmuster",
    "FACE_SIGNAL_NOISE_RATIO": "Minimales Rauschverhältnis Gesicht zu Hintergrund",
    "FACE_SIGNAL_PARALLAX": "Minimale Parallaxe bei der Kopfdrehung",
    "FLASH_COLORS": "Farben des Blitzes",
    "FLASH_TOKEN_INVALID": "Der Blitz dieser Aufgabe ist abgelaufen oder ungültig. Fordern Sie eine neue Aufgabe an.",
    "IDENTIFICATION_SUCCESS": "Identität bestätigt",
    "IMAGE_REQUIRED": "Senden Sie mindestens eine Aufnahme",
    "IMAGE_VALID": "Gültiges Bild",
    "INVALID_FRAME_COUNT": "Senden Sie zwischen {min} und {max} frontale Aufnahmen",
    "LIVENESS_NOT_REQUIRED": "Lebenderkennung nicht erforderlich",
    "PHOTO_ERROR": "Foto {number}: {message}",
    "QR_FACE_MISMATCH": "Das Gesicht gehört nicht zum Inhaber des QR-Codes",
    "QR_HOLDER_FOUND": "Prüfen Sie jetzt das Gesicht von {name}",
    "QR_NOT_FOUND": "QR-Code nicht gefunden",
    "QR_REQUIRED": "Scannen Sie zuerst den QR-Code des Mitarbeiters",
    "QR_WITHOUT_ATTENDANCE": (
        "Per QR-Code identifiziert. Um die Anwesenheit zu erfassen, identifizieren Sie sich mit Ihrem Gesicht."
    ),
    "REJECTION_REASON_REQUIRED": "Geben Sie den Grund für die Ablehnung an",
    "SIGNATURE_INVALID": "Die Signatur dieses Geräts ist ungültig. Melden Sie sich erneut an.",
    "SIGNATURE_KEY_MISMATCH": (
        "Dies ist nicht das Gerät, mit dem Sie sich angemeldet haben. Melden Sie sich auf diesem Gerät erneut an."
    ),
    "SIGNATURE_REQUIRED": (
        "Dieses Prüfgerät muss jede Identifizierung mit seinem Gerät signieren. Verwenden Sie die App auf einem "
        "freigegebenen Gerät."
    ),
    "SIGNATURE_STALE": "Die Signatur dieses Geräts ist abgelaufen. Versuchen Sie es erneut.",
    "SPEECH_SERVICE_UNAVAILABLE": "Der Sprachdienst ist nicht verfügbar. Versuchen Sie es in einigen Minuten erneut.",
    "VALIDATOR_METHOD_NOT_ALLOWED": "Dieses Prüfgerät identifiziert im Modus „{mode}“",
    "VALIDATOR_REQUIRED": "Dieses Konto ist kein Prüfgerät für Identitäten",
    "VERIFICATION_LOCATION_INVALID": (
        "Ihre Position ist ungültig oder nicht genau genug. Aktivieren Sie die genaue Position (GPS) und versuchen Sie "
        "es erneut."
    ),
    "VERIFICATION_LOCATION_REQUIRED": (
        "Ihre Position wird benötigt, um Ihre Identität zu verifizieren. Erlauben Sie den Standortzugriff und "
        "versuchen Sie es erneut."
    ),
    "VIDEO_FACE_MISMATCH": (
        "Das Gesicht im Video stimmt nicht mit Ihren Fotos überein. Halten Sie Ihr Gesicht vor die Kamera und "
        "antworten Sie erneut."
    ),
    "VIDEO_TOO_LARGE": "Das Video ist zu groß (höchstens {size})",
    "VIDEO_UNSUPPORTED_FORMAT": (
        "Das Video konnte nicht gelesen werden. Verwenden Sie eine aktuelle Version von Chrome, Safari, Edge oder "
        "Firefox."
    ),
    "VOICE_CLIP_FOUND": "Video der Antwort",
    "VOICE_CLIP_NOT_FOUND": "Video nicht gefunden",
    "VOICE_NOT_PENDING": "Für diese Registrierung steht keine Stimmprüfung aus",
    "VOICE_RETRIES_EXHAUSTED": (
        "Die Versuche der Stimmprüfung sind aufgebraucht. Wiederholen Sie das erste Foto und die Aufnahmen."
    ),
    "VOICE_SESSION_EXPIRED": (
        "Die Stimmprüfung ist abgelaufen. Öffnen Sie sie erneut: Ihre angenommenen Antworten bleiben erhalten."
    ),
    "VOICE_SESSION_INVALID": (
        "Die Stimmprüfung ist ungültig. Öffnen Sie sie erneut: Ihre angenommenen Antworten bleiben erhalten."
    ),
    "VOICE_SESSION_STARTED": "Videofragen bereit",
    "VOICE_SUM_PROMPT": "Wie viel ist {a} plus {b}?",
}
