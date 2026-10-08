"""Plataforma (ADMIN) e integraciones: empresas, política de verificación, errores, rendimiento, consumo, llaves de la
API y casos de fraude.

Textos en italiano (it-IT), traducidos del sentido de es-MX: misma llave, mismos `{parámetros}` y mismas formas de
plural en todos los idiomas (`tests/test_i18n.py`). «Política de verificación» es «criteri di verifica» (plural en
italiano, como en el glosario)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ACCOUNT_DELETED_LABEL": "Account #{id} (non esiste più)",
    "ACCOUNT_LABEL": "{email} ({role})",
    "API_DEVICE_KEY_INVALID": "La chiave del dispositivo non è valida",
    "API_DEVICE_PROOF_INVALID": "Impossibile verificare il dispositivo. Richiedi una nuova sfida.",
    "API_DEVICE_PROOF_REQUIRED": "Manca la prova del dispositivo: la chiave, la sfida e la firma",
    "API_EMPLOYEE_REFERENCE_INVALID": "Invia l'identificativo o il numero del dipendente, solo uno dei due",
    "API_KEYS_LISTED": {
        "one": "{count} chiave",
        "other": "{count} chiavi",
    },
    "API_KEY_ACCESS_DISABLED": "L'azienda di questa chiave non ha accesso alle API",
    "API_KEY_COMPANY_INACTIVE": "L'azienda di questa chiave è disattivata",
    "API_KEY_COMPANY_SUSPENDED": "L'azienda di questa chiave è sospesa",
    "API_KEY_CREATED": "Chiave creata",
    "API_KEY_EXPIRED": "Questa chiave API è scaduta",
    "API_KEY_INVALID": "La chiave API non è valida",
    "API_KEY_LIMIT": "La tua azienda ha già {count} chiavi non revocate. Revoca quelle che non usi.",
    "API_KEY_NAME_REQUIRED": "Scrivi un nome per la chiave",
    "API_KEY_NOT_FOUND": "Chiave non trovata",
    "API_KEY_REQUIRED": "Manca la chiave API: inviala nell'intestazione X-API-Key",
    "API_KEY_REVOKED": "Questa chiave API è stata revocata",
    "API_KEY_REVOKED_DONE": "Chiave revocata",
    "API_KEY_REVOKED_ROTATE": "Una chiave revocata non può essere ruotata: creane una nuova",
    "API_KEY_ROTATED": "Chiave ruotata",
    "API_KEY_VALIDATORS_DISABLED": "L'azienda di questa chiave non ha il modulo dei validatori",
    "API_SCOPE_INVALID": "Autorizzazioni non valide: {scopes}",
    "API_SCOPE_REQUIRED": "Questa chiave non ha l'autorizzazione «{scope}». Chiedila alla tua azienda (Integrazioni).",
    "ATTENDANCE_FEED": {
        "one": "{count} identificazione",
        "other": "{count} identificazioni",
    },
    "ATTENDANCE_LISTED": {
        "one": "{count} identificazione",
        "other": "{count} identificazioni",
    },
    "CASE_STATUS_FILTER_INVALID": "Scegli uno degli stati del caso disponibili",
    "CLIENT_ERROR_RECORDED": "Malfunzionamento registrato",
    "COMPANIES_LISTED": {
        "one": "{count} azienda trovata",
        "other": "{count} aziende trovate",
    },
    "COMPANY_ADMINS_LISTED": {
        "one": "{count} amministratore",
        "other": "{count} amministratori",
    },
    "COMPANY_ADMIN_CREATED": "Amministratore aggiunto",
    "COMPANY_ADMIN_FOUND": "Amministratore trovato",
    "COMPANY_ADMIN_PASSWORD_RESET": "Password reimpostata",
    "COMPANY_ADMIN_STATUS_UPDATED": "Amministratore aggiornato",
    "COMPANY_CREATED": "Azienda registrata",
    "COMPANY_DELETED": "Azienda eliminata",
    "COMPANY_EMPLOYEES": {
        "one": "{count} dipendente",
        "other": "{count} dipendenti",
    },
    "COMPANY_FOUND": "Azienda trovata",
    "COMPANY_RESTORED": "Azienda ripristinata",
    "COMPANY_UPDATED": "Azienda aggiornata",
    "COMPANY_USAGE": "Consumo dell'azienda",
    "DRIFT_COMPANIES": {
        "one": "{count} azienda",
        "other": "{count} aziende",
    },
    "DRIFT_COMPUTED": {
        "zero": "Deriva calcolata: nessun tentativo nella finestra",
        "one": "Deriva calcolata ({count} riga)",
        "other": "Deriva calcolata ({count} righe)",
    },
    "DRIFT_DISABLED": "Il monitoraggio della deriva è spento nella configurazione del server",
    "DRIFT_SIGNALS": {
        "one": "{count} segnale",
        "other": "{count} segnali",
    },
    "DRIFT_SUMMARY": "Riepilogo della deriva dei segnali",
    "ERRORS_RESOLVED": {
        "one": "{count} errore risolto",
        "other": "{count} errori risolti",
    },
    "ERROR_FILTER_REQUIRED": "Filtra per stato o gravità per segnare gli errori come risolti",
    "ERROR_FILTER_RESOLVED": "Questi errori sono già risolti",
    "ERROR_OCCURRENCES_LISTED": {
        "one": "{count} occorrenza",
        "other": "{count} occorrenze",
    },
    "ERROR_REPORTS_LISTED": {
        "one": "{count} errore",
        "other": "{count} errori",
    },
    "ERROR_REPORT_FOUND": "Errore trovato",
    "ERROR_REPORT_NOT_FOUND": "Errore non trovato",
    "ERROR_STATUS_UPDATED": "Stato aggiornato",
    "ERROR_SUMMARY": "Riepilogo degli errori",
    "FACE_LEARNING_FORGOTTEN": {
        "zero": "Nessun campione appreso: viene confrontato solo con la sua registrazione approvata",
        "one": "Apprendimento azzerato ({count} campione): viene confrontato solo con la sua registrazione approvata",
        "other": (
            "Apprendimento azzerato ({count} campioni): viene confrontato solo con la sua registrazione approvata"
        ),
    },
    "FACE_LEARNING_SUMMARY": "Evoluzione del riconoscimento facciale",
    "FRAUD_CASE": "Caso di frode",
    "FRAUD_CASES": {
        "one": "{count} caso",
        "other": "{count} casi",
    },
    "FRAUD_CASES_COUNT": "Casi di frode attivi",
    "FRAUD_CASE_DECIDED": "Caso aggiornato",
    "FRAUD_CASE_NOTE": "Nota aggiunta",
    "FRAUD_CASE_NOT_FOUND": "Caso di frode non trovato",
    "FRAUD_CASE_SAME_STATUS": "Il caso è già in questo stato",
    "FRAUD_EVIDENCE": "Prove",
    "FRAUD_EVIDENCE_NOT_FOUND": "Le prove non sono più disponibili",
    "FRAUD_NOTE_REQUIRED": "Spiega perché confermi o scarti la frode",
    "FRAUD_NOTE_TEXT_REQUIRED": "Scrivi la nota",
    "INTEGRATION_COMPANY": "Azienda della chiave",
    "INVALID_ANTISPOOF_LEVEL": "Scegli uno dei livelli di rilevamento della contraffazione disponibili",
    "INVALID_CASE_STATUS": "Scegli il nuovo stato del caso",
    "INVALID_CONFIDENCE_LEVEL": "Scegli uno dei livelli di confidenza disponibili",
    "INVALID_CURSOR": "Il cursore non è valido",
    "INVALID_DEVICE_MODE": "Scegli una delle modalità del dispositivo disponibili",
    "INVALID_FLASH_MODE": "Scegli una delle modalità del lampo disponibili",
    "INVALID_POLICY_PRESET": "Scegli uno dei livelli predefiniti",
    "INVALID_RANGE": "La data iniziale non può essere successiva a quella finale",
    "INVALID_RISK_ACTION": "Scegli una delle azioni disponibili",
    "INVALID_RISK_FALLBACK": (
        "Se il motore non funziona, scegli consentire, avvisare o richiedere un passaggio in più"
    ),
    "INVALID_RISK_SIGNAL": "Questo segnale di rischio non esiste",
    "INVALID_SIGNAL_MODE": "Scegli una delle modalità del segnale",
    "INVALID_SINCE_UNTIL": "`since` deve essere precedente a `until`",
    "INVALID_VOICE_PROFILE": "Scegli una delle voci disponibili",
    "LIVENESS_MOVES_MIN": "La prova di vita richiede almeno due movimenti della testa attivi",
    "PERFORMANCE_METRICS": {
        "one": "{count} elemento",
        "other": "{count} elementi",
    },
    "PERFORMANCE_OVERVIEW": "Prestazioni della piattaforma",
    "PERFORMANCE_SERIES": "Serie delle prestazioni",
    "PERFORMANCE_STATEMENTS": {
        "one": "{count} interrogazione",
        "other": "{count} interrogazioni",
    },
    "PERFORMANCE_WEB_VITALS": {
        "one": "{count} schermata",
        "other": "{count} schermate",
    },
    "PLATFORM_STATS": "Indicatori della piattaforma",
    "POLICY": "Criteri di verifica",
    "POLICY_CHANGED_SINCE": "I criteri sono cambiati dopo la richiesta: occorre richiedere di nuovo la modifica",
    "POLICY_CHANGES": {
        "one": "{count} modifica",
        "other": "{count} modifiche",
    },
    "POLICY_CHANGE_APPROVED": "Modifica approvata: ora i criteri la applicano",
    "POLICY_CHANGE_CANCELLED": "Modifica annullata",
    "POLICY_CHANGE_EXPIRED": "Scaduta senza approvazione",
    "POLICY_CHANGE_NOT_FOUND": "Modifica dei criteri non trovata",
    "POLICY_CHANGE_NOT_PENDING": "Questa modifica è già stata decisa",
    "POLICY_CHANGE_PENDING": (
        "La modifica riduce la sicurezza: verrà applicata quando un altro amministratore la approverà"
    ),
    "POLICY_CHANGE_REJECTED": "Modifica rifiutata",
    "POLICY_NOT_REQUESTER": "Solo chi ha richiesto la modifica può annullarla",
    "POLICY_SELF_APPROVAL": "Non puoi approvare una tua modifica: deve farlo un altro amministratore",
    "POLICY_SELF_DECISION": "Non puoi rifiutare una tua modifica: usa «Ritira»",
    "POLICY_SIMULATED": {
        "one": "{count} tentativo simulato",
        "other": "{count} tentativi simulati",
    },
    "POLICY_UNCHANGED": "Nessuna modifica ai criteri",
    "POLICY_UPDATED": "Criteri di verifica aggiornati",
    "RANGE_TOO_LONG": "Scegli un intervallo di massimo un anno",
    "RESTORE_COMPANY_TAX_ID_TAKEN": (
        "Impossibile ripristinare: un'altra azienda ha già questo identificativo fiscale ({name} {value})"
    ),
    "RISK_SCORES_ORDER": "Le soglie di rischio devono essere in ordine: medio < alto < critico",
    "SERVER_STATUS": "Stato del server",
    "SIGNAL_MEASURE_ONLY": (
        "Questo segnale viene solo misurato: non può essere reso obbligatorio finché non è calibrato"
    ),
    "SLOW_ALERTS_LISTED": {
        "one": "{count} avviso",
        "other": "{count} avvisi",
    },
    "SLOW_ALERTS_SUMMARY": "Riepilogo degli avvisi",
    "SLOW_ALERT_FOUND": "Avviso trovato",
    "SLOW_ALERT_NOT_FOUND": "Avviso non trovato",
    "SLOW_ALERT_STATUS_UPDATED": "Stato aggiornato",
    "STORAGE_CLIENT_FAILED": "impossibile connettersi all'archiviazione di Google",
    "STORAGE_COMPANY_DOCUMENTS": "Documenti delle aziende",
    "STORAGE_EMPLOYEE_DOCUMENTS": "Documenti d’identità dei dipendenti",
    "STORAGE_ENROLLMENT_VOICE_CLIPS": "Video della verifica vocale della registrazione del volto",
    "STORAGE_FACE_ENROLLMENT_DRAFT_PHOTOS": "Prime foto della registrazione del volto (bozze)",
    "STORAGE_FACE_ENROLLMENT_PHOTOS": "Foto di riferimento della registrazione del volto",
    "STORAGE_FRAUD_EVIDENCE": "Fotogrammi di prova dei casi di frode",
    "STORAGE_KEY_INVALID": "la chiave dell'account di servizio non è valida",
    "STORAGE_KEY_NOT_MOUNTED": "la chiave dell'account di servizio non è ancora montata (file vuoto)",
    "STORAGE_KEY_UNREADABLE": "impossibile leggere la chiave dell'account di servizio ({error})",
    "STORAGE_NOT_CONFIGURED": "mancano GCS_BUCKET o GCS_CREDENTIALS_FILE nella configurazione",
    "STORAGE_PAYMENT_RECEIPTS": "Ricevute di pagamento",
    "STORAGE_PENDING_DELETIONS": "Oggetti da eliminare dall'archivio",
    "STORAGE_USER_AVATARS": "Foto del profilo (un oggetto per dimensione)",
    "THRESHOLDS_RECALIBRATED": {
        "zero": "Soglie ricalcolate: nessuna modifica",
        "one": "Soglie ricalcolate ({count} modificata)",
        "other": "Soglie ricalcolate ({count} modificate)",
    },
    "TOO_MANY_SAMPLES": "Un lotto ammette al massimo {count} campioni",
    "USAGE_COMPANIES": {
        "one": "{count} azienda",
        "other": "{count} aziende",
    },
    "USAGE_OVERVIEW": "Consumo della piattaforma",
    "USAGE_ROUTES": {
        "one": "{count} percorso",
        "other": "{count} percorsi",
    },
    "USAGE_USERS": {
        "one": "{count} account",
        "other": "{count} account",
    },
    "VALIDATORS_DISABLED": (
        "La tua azienda non ha il modulo dei validatori. Richiedilo all'amministratore della piattaforma."
    ),
    "VALIDATORS_LISTED": {
        "one": "{count} validatore",
        "other": "{count} validatori",
    },
    "VALIDATOR_LIMIT_BELOW_ACTIVE": {
        "one": (
            "L'azienda ha {count} validatore attivo: il limite non può essere inferiore. Chiedile di disattivarlo "
            "prima."
        ),
        "other": (
            "L'azienda ha {count} validatori attivi: il limite non può essere inferiore. Chiedile di disattivare "
            "quelli in eccesso."
        ),
    },
    "VALIDATOR_LIMIT_REACHED": {
        "zero": (
            "La tua azienda non ha più posti per i validatori. Chiedine altri all'amministratore della piattaforma."
        ),
        "one": (
            "La tua azienda ha raggiunto il limite di {count} validatore attivo. Disattivane uno o chiedine altri "
            "all'amministratore della piattaforma."
        ),
        "other": (
            "La tua azienda ha raggiunto il limite di {count} validatori attivi. Disattivane uno o chiedine altri "
            "all'amministratore della piattaforma."
        ),
    },
    "WEB_PERFORMANCE_RECORDED": "Prestazioni registrate",
}
