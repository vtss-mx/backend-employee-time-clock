"""Plataforma (ADMIN) e integraciones: empresas, política de verificación, errores, rendimiento, consumo, llaves de la
API y casos de fraude.

Textos en alemán de Alemania (de-DE, trato de «Sie»): misma llave, mismos `{parámetros}` y mismas formas de plural
en todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ACCOUNT_DELETED_LABEL": "Konto #{id} (existiert nicht mehr)",
    "ACCOUNT_LABEL": "{email} ({role})",
    "API_DEVICE_KEY_INVALID": "Der Geräteschlüssel ist ungültig",
    "API_DEVICE_PROOF_INVALID": "Das Gerät konnte nicht verifiziert werden. Fordern Sie eine neue Aufgabe an.",
    "API_DEVICE_PROOF_REQUIRED": "Der Gerätenachweis fehlt: Schlüssel, Aufgabe und Signatur",
    "API_EMPLOYEE_REFERENCE_INVALID": "Senden Sie die Kennung oder die Nummer des Mitarbeiters, nur eines von beiden",
    "API_KEYS_LISTED": {
        "one": "{count} Schlüssel",
        "other": "{count} Schlüssel",
    },
    "API_KEY_ACCESS_DISABLED": "Das Unternehmen dieses Schlüssels hat keinen Zugriff auf die API",
    "API_KEY_COMPANY_INACTIVE": "Das Unternehmen dieses Schlüssels ist deaktiviert",
    "API_KEY_COMPANY_SUSPENDED": "Das Unternehmen dieses Schlüssels ist gesperrt",
    "API_KEY_CREATED": "Schlüssel erstellt",
    "API_KEY_EXPIRED": "Dieser API-Schlüssel ist abgelaufen",
    "API_KEY_INVALID": "Der API-Schlüssel ist ungültig",
    "API_KEY_LIMIT": (
        "Ihr Unternehmen hat bereits {count} nicht widerrufene Schlüssel. Widerrufen Sie die Schlüssel, die Sie nicht "
        "verwenden."
    ),
    "API_KEY_NAME_REQUIRED": "Geben Sie einen Namen für den Schlüssel ein",
    "API_KEY_NOT_FOUND": "Schlüssel nicht gefunden",
    "API_KEY_REQUIRED": "Der API-Schlüssel fehlt: Senden Sie ihn in der Kopfzeile X-API-Key",
    "API_KEY_REVOKED": "Dieser API-Schlüssel wurde widerrufen",
    "API_KEY_REVOKED_DONE": "Schlüssel widerrufen",
    "API_KEY_REVOKED_ROTATE": "Ein widerrufener Schlüssel kann nicht erneuert werden: Erstellen Sie einen neuen",
    "API_KEY_ROTATED": "Schlüssel erneuert",
    "API_KEY_VALIDATORS_DISABLED": "Das Unternehmen dieses Schlüssels hat das Modul für Prüfgeräte nicht",
    "API_SCOPE_INVALID": "Ungültige Berechtigungen: {scopes}",
    "API_SCOPE_REQUIRED": (
        "Dieser Schlüssel hat die Berechtigung „{scope}“ nicht. Fordern Sie sie bei Ihrem Unternehmen an "
        "(Integrationen)."
    ),
    "ATTENDANCE_FEED": {
        "one": "{count} Identifizierung",
        "other": "{count} Identifizierungen",
    },
    "ATTENDANCE_LISTED": {
        "one": "{count} Identifizierung",
        "other": "{count} Identifizierungen",
    },
    "CASE_STATUS_FILTER_INVALID": "Wählen Sie einen der verfügbaren Status für Fälle",
    "CLIENT_ERROR_RECORDED": "Fehler erfasst",
    "COMPANIES_LISTED": {
        "one": "{count} Unternehmen gefunden",
        "other": "{count} Unternehmen gefunden",
    },
    "COMPANY_ADMINS_LISTED": {
        "one": "{count} Administrator",
        "other": "{count} Administratoren",
    },
    "COMPANY_ADMIN_CREATED": "Administrator hinzugefügt",
    "COMPANY_ADMIN_FOUND": "Administrator gefunden",
    "COMPANY_ADMIN_PASSWORD_RESET": "Passwort zurückgesetzt",
    "COMPANY_ADMIN_STATUS_UPDATED": "Administrator aktualisiert",
    "COMPANY_CREATED": "Unternehmen registriert",
    "COMPANY_DELETED": "Unternehmen gelöscht",
    "COMPANY_EMPLOYEES": {
        "one": "{count} Mitarbeiter",
        "other": "{count} Mitarbeiter",
    },
    "COMPANY_FOUND": "Unternehmen gefunden",
    "COMPANY_RESTORED": "Unternehmen wiederhergestellt",
    "COMPANY_UPDATED": "Unternehmen aktualisiert",
    "COMPANY_USAGE": "Verbrauch des Unternehmens",
    "DRIFT_COMPANIES": {
        "one": "{count} Unternehmen",
        "other": "{count} Unternehmen",
    },
    "DRIFT_COMPUTED": {
        "zero": "Drift berechnet: keine Versuche im Fenster",
        "one": "Drift berechnet ({count} Zeile)",
        "other": "Drift berechnet ({count} Zeilen)",
    },
    "DRIFT_DISABLED": "Die Drift-Überwachung ist in der Serverkonfiguration ausgeschaltet",
    "DRIFT_SIGNALS": {
        "one": "{count} Signal",
        "other": "{count} Signale",
    },
    "DRIFT_SUMMARY": "Übersicht der Drift der Signale",
    "ERRORS_RESOLVED": {
        "one": "{count} Fehler behoben",
        "other": "{count} Fehler behoben",
    },
    "ERROR_FILTER_REQUIRED": "Filtern Sie nach Status oder Schweregrad, um Fehler als behoben zu markieren",
    "ERROR_FILTER_RESOLVED": "Diese Fehler sind bereits behoben",
    "ERROR_OCCURRENCES_LISTED": {
        "one": "{count} Vorkommen",
        "other": "{count} Vorkommen",
    },
    "ERROR_REPORTS_LISTED": {
        "one": "{count} Fehler",
        "other": "{count} Fehler",
    },
    "ERROR_REPORT_FOUND": "Fehler gefunden",
    "ERROR_REPORT_NOT_FOUND": "Fehler nicht gefunden",
    "ERROR_STATUS_UPDATED": "Nachverfolgung aktualisiert",
    "ERROR_SUMMARY": "Fehlerübersicht",
    "FACE_LEARNING_FORGOTTEN": {
        "zero": "Keine gelernten Proben: Verglichen wird nur mit der freigegebenen Registrierung",
        "one": "Lernen zurückgesetzt ({count} Probe): Verglichen wird nur mit der freigegebenen Registrierung",
        "other": "Lernen zurückgesetzt ({count} Proben): Verglichen wird nur mit der freigegebenen Registrierung",
    },
    "FACE_LEARNING_SUMMARY": "Entwicklung der Gesichtserkennung",
    "FRAUD_CASE": "Betrugsfall",
    "FRAUD_CASES": {
        "one": "{count} Fall",
        "other": "{count} Fälle",
    },
    "FRAUD_CASES_COUNT": "Aktive Betrugsfälle",
    "FRAUD_CASE_DECIDED": "Fall aktualisiert",
    "FRAUD_CASE_NOTE": "Notiz hinzugefügt",
    "FRAUD_CASE_NOT_FOUND": "Betrugsfall nicht gefunden",
    "FRAUD_CASE_SAME_STATUS": "Der Fall hat diesen Status bereits",
    "FRAUD_EVIDENCE": "Beweismaterial",
    "FRAUD_EVIDENCE_NOT_FOUND": "Das Beweismaterial ist nicht mehr verfügbar",
    "FRAUD_NOTE_REQUIRED": "Begründen Sie, warum Sie den Betrug bestätigen oder verwerfen",
    "FRAUD_NOTE_TEXT_REQUIRED": "Schreiben Sie die Notiz",
    "INTEGRATION_COMPANY": "Unternehmen des Schlüssels",
    "INVALID_ANTISPOOF_LEVEL": "Wählen Sie eine der verfügbaren Stufen der Täuschungserkennung",
    "INVALID_CASE_STATUS": "Wählen Sie den neuen Status des Falls",
    "INVALID_CONFIDENCE_LEVEL": "Wählen Sie eine der verfügbaren Vertrauensstufen",
    "INVALID_CURSOR": "Der Cursor ist ungültig",
    "INVALID_DEVICE_MODE": "Wählen Sie einen der verfügbaren Gerätemodi",
    "INVALID_FLASH_MODE": "Wählen Sie einen der verfügbaren Blitzmodi",
    "INVALID_POLICY_PRESET": "Wählen Sie eine der vordefinierten Stufen",
    "INVALID_RANGE": "Das Startdatum darf nicht nach dem Enddatum liegen",
    "INVALID_RISK_ACTION": "Wählen Sie eine der verfügbaren Aktionen",
    "INVALID_RISK_FALLBACK": (
        "Wählen Sie für einen Ausfall der Risikobewertung: zulassen, warnen oder einen weiteren Schritt verlangen"
    ),
    "INVALID_RISK_SIGNAL": "Dieses Risikosignal existiert nicht",
    "INVALID_SIGNAL_MODE": "Wählen Sie einen der Modi des Signals",
    "INVALID_SINCE_UNTIL": "`since` muss vor `until` liegen",
    "INVALID_VOICE_PROFILE": "Wählen Sie eine der verfügbaren Stimmen",
    "PERFORMANCE_METRICS": {
        "one": "{count} Element",
        "other": "{count} Elemente",
    },
    "PERFORMANCE_OVERVIEW": "Leistung der Plattform",
    "PERFORMANCE_SERIES": "Leistungsverlauf",
    "PERFORMANCE_STATEMENTS": {
        "one": "{count} Abfrage",
        "other": "{count} Abfragen",
    },
    "PERFORMANCE_WEB_VITALS": {
        "one": "{count} Ansicht",
        "other": "{count} Ansichten",
    },
    "PLATFORM_STATS": "Kennzahlen der Plattform",
    "POLICY": "Prüfrichtlinie",
    "POLICY_CHANGED_SINCE": (
        "Die Richtlinie wurde nach dem Antrag geändert: Die Änderung muss erneut beantragt werden"
    ),
    "POLICY_CHANGES": {
        "one": "{count} Änderung",
        "other": "{count} Änderungen",
    },
    "POLICY_CHANGE_APPROVED": "Änderung genehmigt: Die Richtlinie wendet sie bereits an",
    "POLICY_CHANGE_CANCELLED": "Änderung zurückgezogen",
    "POLICY_CHANGE_EXPIRED": "Ohne Genehmigung abgelaufen",
    "POLICY_CHANGE_NOT_FOUND": "Richtlinienänderung nicht gefunden",
    "POLICY_CHANGE_NOT_PENDING": "Über diese Änderung wurde bereits entschieden",
    "POLICY_CHANGE_PENDING": (
        "Die Änderung senkt die Sicherheit: Sie gilt erst, wenn ein anderer Administrator sie genehmigt"
    ),
    "POLICY_CHANGE_REJECTED": "Änderung abgelehnt",
    "POLICY_NOT_REQUESTER": "Nur die Person, die die Änderung beantragt hat, kann sie zurückziehen",
    "POLICY_SELF_APPROVAL": (
        "Sie können Ihre eigene Änderung nicht genehmigen: Ein anderer Administrator muss das übernehmen"
    ),
    "POLICY_SELF_DECISION": "Sie können Ihre eigene Änderung nicht ablehnen: Verwenden Sie „Zurückziehen“",
    "POLICY_SIMULATED": {
        "one": "{count} Versuch simuliert",
        "other": "{count} Versuche simuliert",
    },
    "POLICY_UNCHANGED": "Keine Änderungen an der Richtlinie",
    "POLICY_UPDATED": "Prüfrichtlinie aktualisiert",
    "RANGE_TOO_LONG": "Wählen Sie einen Zeitraum von höchstens einem Jahr",
    "RESTORE_COMPANY_TAX_ID_TAKEN": (
        "Wiederherstellen nicht möglich: Ein anderes Unternehmen hat bereits diese Steuernummer ({name} {value})"
    ),
    "RISK_SCORES_ORDER": "Die Risikogrenzen müssen aufsteigend sein: mittel < hoch < kritisch",
    "SERVER_STATUS": "Serverstatus",
    "SIGNAL_MEASURE_ONLY": "Dieses Signal wird nur gemessen: Es kann erst nach der Kalibrierung verlangt werden",
    "SLOW_ALERTS_LISTED": {
        "one": "{count} Warnung",
        "other": "{count} Warnungen",
    },
    "SLOW_ALERTS_SUMMARY": "Übersicht der Warnungen",
    "SLOW_ALERT_FOUND": "Warnung gefunden",
    "SLOW_ALERT_NOT_FOUND": "Warnung nicht gefunden",
    "SLOW_ALERT_STATUS_UPDATED": "Nachverfolgung aktualisiert",
    "STORAGE_CLIENT_FAILED": "keine Verbindung zum Speicher von Google möglich",
    "STORAGE_COMPANY_DOCUMENTS": "Dokumente der Unternehmen",
    "STORAGE_EMPLOYEE_DOCUMENTS": "Ausweisdokumente der Mitarbeiter",
    "STORAGE_ENROLLMENT_VOICE_CLIPS": "Videos der Stimmprüfung bei der Gesichtsregistrierung",
    "STORAGE_FACE_ENROLLMENT_DRAFT_PHOTOS": "Erste Fotos der Gesichtsregistrierung (Entwürfe)",
    "STORAGE_FACE_ENROLLMENT_PHOTOS": "Referenzfotos der Gesichtsregistrierung",
    "STORAGE_FRAUD_EVIDENCE": "Einzelbilder als Beweismaterial aus Betrugsfällen",
    "STORAGE_KEY_INVALID": "der Schlüssel des Dienstkontos ist ungültig",
    "STORAGE_KEY_NOT_MOUNTED": "der Schlüssel des Dienstkontos ist noch nicht eingebunden (leere Datei)",
    "STORAGE_KEY_UNREADABLE": "der Schlüssel des Dienstkontos konnte nicht gelesen werden ({error})",
    "STORAGE_NOT_CONFIGURED": "GCS_BUCKET oder GCS_CREDENTIALS_FILE fehlt in der Konfiguration",
    "STORAGE_PAYMENT_RECEIPTS": "Zahlungsbelege",
    "STORAGE_PENDING_DELETIONS": "Objekte, die aus dem Speicher gelöscht werden müssen",
    "STORAGE_USER_AVATARS": "Profilfotos (ein Objekt pro Größe)",
    "THRESHOLDS_RECALIBRATED": {
        "zero": "Schwellenwerte neu berechnet: keine Änderungen",
        "one": "Schwellenwerte neu berechnet ({count} geändert)",
        "other": "Schwellenwerte neu berechnet ({count} geändert)",
    },
    "TOO_MANY_SAMPLES": "Ein Stapel darf höchstens {count} Messwerte enthalten",
    "USAGE_COMPANIES": {
        "one": "{count} Unternehmen",
        "other": "{count} Unternehmen",
    },
    "USAGE_OVERVIEW": "Verbrauch der Plattform",
    "USAGE_ROUTES": {
        "one": "{count} Route",
        "other": "{count} Routen",
    },
    "USAGE_USERS": {
        "one": "{count} Konto",
        "other": "{count} Konten",
    },
    "VALIDATORS_DISABLED": (
        "Ihr Unternehmen hat das Modul für Prüfgeräte nicht. Fordern Sie es beim Administrator der Plattform an."
    ),
    "VALIDATORS_LISTED": {
        "one": "{count} Prüfgerät",
        "other": "{count} Prüfgeräte",
    },
    "VALIDATOR_LIMIT_BELOW_ACTIVE": {
        "one": (
            "Das Unternehmen hat {count} aktives Prüfgerät: Das Limit kann nicht niedriger sein. Bitten Sie das "
            "Unternehmen, es zuerst zu deaktivieren."
        ),
        "other": (
            "Das Unternehmen hat {count} aktive Prüfgeräte: Das Limit kann nicht niedriger sein. Bitten Sie das "
            "Unternehmen, die überzähligen zu deaktivieren."
        ),
    },
    "VALIDATOR_LIMIT_REACHED": {
        "zero": (
            "Ihr Unternehmen hat keine freien Plätze für Prüfgeräte mehr. Fordern Sie beim Administrator der "
            "Plattform weitere an."
        ),
        "one": (
            "Ihr Unternehmen hat sein Limit von {count} aktivem Prüfgerät erreicht. Deaktivieren Sie eines oder "
            "fordern Sie beim Administrator der Plattform weitere an."
        ),
        "other": (
            "Ihr Unternehmen hat sein Limit von {count} aktiven Prüfgeräten erreicht. Deaktivieren Sie eines oder "
            "fordern Sie beim Administrator der Plattform weitere an."
        ),
    },
    "WEB_PERFORMANCE_RECORDED": "Leistung erfasst",
}
