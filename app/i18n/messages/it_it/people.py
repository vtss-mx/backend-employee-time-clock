"""Personal y empresas: empleados, cuentas compartidas, departamentos, validadores, documentos (RFC, CURP, NSS),
teléfonos y domicilios.

Textos en italiano (it-IT), traducidos del sentido de es-MX: misma llave, mismos `{parámetros}` y mismas formas de
plural en todos los idiomas (`tests/test_i18n.py`). Las siglas mexicanas (RFC, CURP, NSS) se nombran igual: «l'RFC»,
«il CURP», «l'NSS» (el artículo italiano sigue a cómo se lee la sigla)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ACCOUNT_EMAIL_TAKEN": "L'indirizzo e-mail è già registrato",
    "ACCOUNT_LINKABLE_EMAIL": (
        "Questa persona ha già un account sulla piattaforma. Verrà aggiunta alla tua azienda con la sua password "
        "attuale."
    ),
    "ACCOUNT_LINKABLE_PHONE": "Questo telefono appartiene a un account esistente. Usa l'e-mail di quella persona.",
    "ACCOUNT_PHONE_MATCHES": (
        "Il telefono corrisponde all'account di questa persona. Verrà collegata alla tua azienda."
    ),
    "ACCOUNT_PHONE_MISMATCH": (
        "Questa e-mail ha già un account con un altro telefono. Verifica il telefono della persona."
    ),
    "ACCOUNT_PHONE_MISSING": (
        "L'account di questa persona non ha un telefono. La sua azienda attuale deve aggiungerlo prima di poterla "
        "collegare."
    ),
    "ACCOUNT_PHONE_TAKEN": "Il telefono è già registrato in un altro account",
    "ADDRESS_CITY_REQUIRED": "La città è obbligatoria",
    "ADDRESS_CITY_TOO_SHORT": "Il nome della città è troppo corto",
    "ADDRESS_EXTERIOR_NUMBER_REQUIRED": "Il numero civico è obbligatorio",
    "ADDRESS_MUNICIPALITY_REQUIRED": "Il comune è obbligatorio",
    "ADDRESS_MUNICIPALITY_TOO_SHORT": "Il nome del comune è troppo corto",
    "ADDRESS_NEIGHBORHOOD_REQUIRED": "Il quartiere è obbligatorio",
    "ADDRESS_NEIGHBORHOOD_TOO_SHORT": "Il nome del quartiere è troppo corto",
    "ADDRESS_STATE_REQUIRED": "Lo stato è obbligatorio",
    "ADDRESS_STATE_TOO_SHORT": "Il nome dello stato è troppo corto",
    "ADDRESS_STREET_REQUIRED": "La via è obbligatoria",
    "ADDRESS_STREET_TOO_SHORT": "Il nome della via è troppo corto",
    "ALREADY_EMPLOYEE": "Questa persona è già un dipendente della tua azienda",
    "BIRTH_DATE_INVALID": "La data di nascita non è valida",
    "BIRTH_DATE_NOT_PAST": "La data di nascita deve essere precedente a oggi",
    "COMPANY_ACTIVATED": "Azienda attivata",
    "COMPANY_ADMIN_NOT_FOUND": "Amministratore non trovato",
    "COMPANY_DEACTIVATED": "Azienda disattivata",
    "COMPANY_DUPLICATE": "L'identificativo fiscale o l'e-mail sono già registrati",
    "COMPANY_HAS_BILLING": (
        "L'azienda ha addebiti o pagamenti registrati: disattivala o sospendila invece di eliminarla"
    ),
    "COMPANY_HAS_EMPLOYEES": "L'azienda ha dipendenti registrati: disattivala invece di eliminarla",
    "COMPANY_NOT_FOUND": "Azienda non trovata",
    "COMPANY_RFC_LENGTH": "L'RFC deve avere 12 caratteri (persona giuridica) o 13 (persona fisica)",
    "COMPANY_TAX_ID_TAKEN": "Esiste già un'azienda con questo identificativo fiscale",
    "COORDINATES_INCOMPLETE": "Indica la latitudine e la longitudine del punto sulla mappa",
    "COUNTRY_INVALID": "Scegli un paese dall'elenco",
    "CURP_AVAILABLE": "CURP disponibile",
    "CURP_BIRTH_DATE_MISMATCH": (
        "Il CURP indica come data di nascita il {document}, ma la data di nascita inserita è il {birth}"
    ),
    "CURP_CENTURY_MISMATCH": (
        "Il CURP non corrisponde al secolo della data di nascita: il suo 17° carattere è un numero per le nascite "
        "prima del 2000 e una lettera dal 2000 in poi"
    ),
    "CURP_CHECK_DIGIT": "Il CURP non è valido: la cifra di controllo non corrisponde",
    "CURP_DATE_INVALID": "La data contenuta nel CURP (AAMMGG) non è valida",
    "CURP_FORMAT": "Il CURP non ha un formato valido (ad es. HEGG560427MVZRRL04)",
    "CURP_LENGTH": {
        "one": "Il CURP ha {length} caratteri; ne è stato inserito {count}",
        "other": "Il CURP ha {length} caratteri; ne sono stati inseriti {count}",
    },
    "CURP_TAKEN": "Il CURP è già registrato per un altro dipendente",
    "DEPARTMENTS_LISTED": {
        "one": "{count} reparto",
        "other": "{count} reparti",
    },
    "DEPARTMENT_CREATED": "Reparto creato",
    "DEPARTMENT_DELETED": "Reparto eliminato",
    "DEPARTMENT_EMPLOYEE_ASSIGNED": "Dipendente assegnato",
    "DEPARTMENT_EMPLOYEE_REMOVED": "Dipendente rimosso dal reparto",
    "DEPARTMENT_FOUND": "Reparto trovato",
    "DEPARTMENT_HAS_EMPLOYEES": "Il reparto ha dipendenti assegnati: riassegnali o rimuovili prima di eliminarlo",
    "DEPARTMENT_MANAGER_ADDED": "Responsabile aggiunto",
    "DEPARTMENT_MANAGER_REMOVED": "Responsabile rimosso",
    "DEPARTMENT_NAME_TAKEN": "Esiste già un reparto con questo nome nella tua azienda",
    "DEPARTMENT_NOT_FOUND": "Reparto non trovato",
    "DEPARTMENT_RESTORED": "Reparto ripristinato",
    "DEPARTMENT_UPDATED": "Reparto aggiornato",
    "DEVICES_LISTED": {
        "one": "{count} dispositivo",
        "other": "{count} dispositivi",
    },
    "DEVICE_STATUS_UPDATED": "Dispositivo aggiornato",
    "DOCUMENT_OPTIONAL": "Facoltativo: può restare vuoto",
    "EMAIL_AVAILABLE": "E-mail disponibile",
    "EMAIL_INVALID": "Indirizzo e-mail non valido",
    "EMAIL_REQUIRED": "L'e-mail è obbligatoria",
    "EMAIL_TAKEN": "L'e-mail è già registrata sulla piattaforma",
    "EMPLOYEES_GONE": "Alcuni dipendenti non esistono più: aggiorna l'elenco",
    "EMPLOYEES_LISTED": {
        "one": "{count} dipendente trovato",
        "other": "{count} dipendenti trovati",
    },
    "EMPLOYEES_ONLY": "Solo i dipendenti possono usare questa funzione",
    "EMPLOYEE_ACTIVATED": "Dipendente attivato",
    "EMPLOYEE_CREATED": "Dipendente registrato",
    "EMPLOYEE_DEACTIVATED": "Dipendente disattivato",
    "EMPLOYEE_DELETED": "Dipendente eliminato",
    "EMPLOYEE_DEVICE_APPROVED": "Dispositivo approvato: non viene più trattato come sconosciuto",
    "EMPLOYEE_DEVICE_REVOKED": "Dispositivo revocato: ora viene trattato come sconosciuto",
    "EMPLOYEE_DUPLICATE": "L'e-mail, il telefono, il numero di matricola, l'RFC, il CURP o l'NSS sono già registrati",
    "EMPLOYEE_FOUND": "Dipendente trovato",
    "EMPLOYEE_IDS": {
        "one": "{count} dipendente nel filtro",
        "other": "{count} dipendenti nel filtro",
    },
    "EMPLOYEE_IDS_REPEATED": "Ogni dipendente deve comparire una sola volta",
    "EMPLOYEE_INACTIVE": "Il dipendente è inattivo",
    "EMPLOYEE_LIMIT_REACHED": (
        "La tua azienda ha raggiunto il limite di {count} dipendenti. Contatta l'amministratore della piattaforma."
    ),
    "EMPLOYEE_NOT_FOUND": "Dipendente non trovato",
    "EMPLOYEE_NUMBER_AVAILABLE": "Numero di matricola disponibile",
    "EMPLOYEE_NUMBER_INVALID": (
        "Il numero di matricola deve avere da 1 a 30 caratteri: lettere, numeri, trattino o trattino basso"
    ),
    "EMPLOYEE_NUMBER_TAKEN": "Il numero di matricola è già registrato",
    "EMPLOYEE_RESTORED": "Dipendente ripristinato. Deve registrare di nuovo il volto.",
    "EMPLOYEE_TOO_YOUNG": "Il dipendente deve avere almeno {count} anni",
    "EMPLOYEE_UPDATED": "Dipendente aggiornato",
    "ENROLLMENT_RESET_BY_COMPANY": "Registrazione azzerata dall'azienda",
    "FACE_ERASED_ON_DELETE": (
        "I tuoi dati facciali sono stati cancellati quando la tua scheda è stata eliminata. Registra di nuovo il volto."
    ),
    "FACE_NOT_APPROVED": "Il dipendente non ha ancora un volto approvato: registralo prima",
    "FACE_NOT_ENROLLED_YET": "Prima devi registrare il tuo volto",
    "FACE_PENDING_REVIEW": "La registrazione del tuo volto è in attesa di convalida da parte della tua azienda",
    "FACE_REJECTED_ENROLL_AGAIN": "La registrazione del tuo volto è stata rifiutata. Registra di nuovo il volto.",
    "FIELD_NOT_ALLOWED": "Non puoi validare questo campo",
    "FIELD_REQUIRED": "Questo campo è obbligatorio",
    "IDENTITY_REVERIFY_REQUESTED": "Il dipendente dovrà registrare di nuovo il volto",
    "IDENTITY_REVERIFY_REQUESTED_ALL": {
        "one": "{count} dipendente dovrà registrare di nuovo il volto",
        "other": "{count} dipendenti dovranno registrare di nuovo il volto",
    },
    "LAST_COMPANY_ADMIN": "L'azienda deve mantenere almeno un amministratore attivo",
    "LEGAL_NAME_REQUIRED": "La ragione sociale è obbligatoria",
    "LOCATION_POINT_REQUIRED": "Per richiedere la posizione, segna sulla mappa il punto dell'indirizzo",
    "LOCATION_RADIUS_REQUIRED": "Indica in metri il raggio entro cui il validatore può accedere",
    "MAX_CHARACTERS": "Massimo {count} caratteri",
    "NAME_AVAILABLE": "Nome disponibile",
    "NAME_INVALID_CHARACTERS": "Sono consentiti solo lettere, spazi, apostrofi, punti e trattini",
    "NAME_REQUIRED": "Il nome è obbligatorio",
    "NAME_TOO_LONG": "Il nome può avere al massimo {count} caratteri",
    "NSS_AVAILABLE": "NSS disponibile",
    "NSS_CHECK_DIGIT": "L'NSS non è valido: la cifra di controllo non corrisponde",
    "NSS_LENGTH": "L'NSS ha {count} cifre",
    "NSS_TAKEN": "L'NSS è già registrato per un altro dipendente",
    "PASSWORD_NEEDS_DIGIT": "La password deve contenere almeno un numero",
    "PASSWORD_NEEDS_LOWERCASE": "La password deve contenere almeno una lettera minuscola",
    "PASSWORD_NEEDS_UPPERCASE": "La password deve contenere almeno una lettera maiuscola",
    "PASSWORD_REQUIRED": "La password è obbligatoria per una nuova persona",
    "PASSWORD_TOO_LONG": "La password non deve superare {count} caratteri",
    "PASSWORD_TOO_SHORT": "La password deve avere almeno {count} caratteri",
    "PHONE_AVAILABLE": "Telefono disponibile",
    "PHONE_COUNTRY_UNAVAILABLE": "Non si accettano telefoni di quel paese",
    "PHONE_INVALID": "Il telefono non è valido",
    "PHONE_INVALID_FOR_CODE": "Il telefono non è valido per il prefisso +{code}",
    "PHONE_REQUIRED": "Il telefono è obbligatorio",
    "PHONE_VALID": "Telefono valido",
    "POSTAL_CODE_INVALID": "Il codice postale non è valido",
    "POSTAL_CODE_MX": "Il codice postale del Messico ha 5 cifre",
    "QR_REVOKED": "Codice QR invalidato",
    "QR_SUMMARY": "Attività del codice QR",
    "RESTORE_CURP_TAKEN": "Impossibile ripristinare: un altro dipendente ha già il CURP {value}",
    "RESTORE_EMPLOYEE_NUMBER_TAKEN": "Impossibile ripristinare: un altro dipendente ha già la matricola {value}",
    "RESTORE_EMPLOYMENT_TAKEN": (
        "Impossibile ripristinare: questa persona ha già un'altra scheda attiva nella tua azienda"
    ),
    "RESTORE_NSS_TAKEN": "Impossibile ripristinare: un altro dipendente ha già l'NSS {value}",
    "RESTORE_RFC_TAKEN": "Impossibile ripristinare: un altro dipendente ha già l'RFC {value}",
    "REVERIFY_DEFAULT_REASON": "La tua azienda ti ha chiesto di verificare di nuovo la tua identità",
    "RFC_AVAILABLE": "RFC disponibile",
    "RFC_BIRTH_DATE_MISMATCH": (
        "L'RFC indica come data di nascita il {document}, ma la data di nascita inserita è il {birth}"
    ),
    "RFC_DATE_INVALID": "La data contenuta nel codice RFC (AAMMGG) non è valida",
    "RFC_FORMAT": "L'RFC non ha un formato valido (ad es. PEGJ900515AB1)",
    "RFC_GENERIC": "L'RFC generico non è valido. Inserisci l'RFC della persona.",
    "RFC_LENGTH": {
        "one": "L'RFC di una persona fisica ha {length} caratteri; ne è stato inserito {count}",
        "other": "L'RFC di una persona fisica ha {length} caratteri; ne sono stati inseriti {count}",
    },
    "RFC_TAKEN": "L'RFC è già registrato per un altro dipendente",
    "SHARED_ACCOUNT": (
        "Questa persona lavora anche in un'altra azienda. La sua e-mail, il telefono e la password non possono essere "
        "modificati da qui."
    ),
    "TAX_ID_AVAILABLE": "Identificativo fiscale disponibile",
    "TAX_ID_CHECK_DIGIT": "La cifra di controllo di {name} non corrisponde. Verifica il numero.",
    "TAX_ID_FORMAT": "Formato di {name} non valido (ad es. {example})",
    "TAX_ID_LENGTH": "Il numero di {name} deve avere da {min} a {max} caratteri",
    "TAX_ID_LENGTH_EXACT": "Il numero di {name} deve avere {length} caratteri",
    "TAX_ID_TYPE_COUNTRY": "{name} non è un identificativo fiscale del paese selezionato ({country})",
    "TAX_ID_TYPE_INVALID": "Scegli un tipo di identificativo dall'elenco",
    "TRADE_NAME_REQUIRED": "Il nome commerciale è obbligatorio",
    "VALIDATION_COMPANY_REQUIRED": "Questa validazione è riservata agli account aziendali",
    "VALIDATOR_CREATED": "Validatore registrato",
    "VALIDATOR_DELETED": "Validatore eliminato",
    "VALIDATOR_FOUND": "Validatore trovato",
    "VALIDATOR_NAME_REQUIRED": "Il nome del validatore è obbligatorio",
    "VALIDATOR_NOT_FOUND": "Validatore non trovato",
    "VALIDATOR_PASSWORD_RESET": "Password reimpostata",
    "VALIDATOR_RESTORED": "Validatore ripristinato",
    "VALIDATOR_STATUS_UPDATED": "Validatore aggiornato",
    "VALIDATOR_UPDATED": "Validatore aggiornato",
    "VERIFICATIONS_LISTED": {
        "one": "{count} tentativo di verifica",
        "other": "{count} tentativi di verifica",
    },
}
