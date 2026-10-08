"""Personal y empresas: empleados, cuentas compartidas, departamentos, validadores, documentos (RFC, CURP, NSS),
teléfonos y domicilios.

Textos en alemán de Alemania (de-DE, trato de «Sie»): misma llave, mismos `{parámetros}` y mismas formas de plural
en todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ACCOUNT_EMAIL_TAKEN": "Die E-Mail-Adresse ist bereits registriert",
    "ACCOUNT_LINKABLE_EMAIL": (
        "Diese Person hat bereits ein Konto auf der Plattform. Sie wird Ihrem Unternehmen mit ihrem aktuellen "
        "Passwort hinzugefügt."
    ),
    "ACCOUNT_LINKABLE_PHONE": (
        "Diese Telefonnummer gehört zu einem bestehenden Konto. Verwenden Sie die E-Mail-Adresse dieser Person."
    ),
    "ACCOUNT_PHONE_MATCHES": (
        "Die Telefonnummer stimmt mit dem Konto dieser Person überein. Sie wird mit Ihrem Unternehmen verknüpft."
    ),
    "ACCOUNT_PHONE_MISMATCH": (
        "Zu dieser E-Mail-Adresse gibt es bereits ein Konto mit einer anderen Telefonnummer. Prüfen Sie die "
        "Telefonnummer der Person."
    ),
    "ACCOUNT_PHONE_MISSING": (
        "Das Konto dieser Person hat keine Telefonnummer. Das aktuelle Unternehmen der Person muss sie hinzufügen, "
        "bevor die Verknüpfung möglich ist."
    ),
    "ACCOUNT_PHONE_TAKEN": "Die Telefonnummer ist bereits bei einem anderen Konto registriert",
    "ADDRESS_CITY_REQUIRED": "Die Stadt ist ein Pflichtfeld",
    "ADDRESS_CITY_TOO_SHORT": "Die Stadt ist zu kurz",
    "ADDRESS_EXTERIOR_NUMBER_REQUIRED": "Die Hausnummer ist ein Pflichtfeld",
    "ADDRESS_MUNICIPALITY_REQUIRED": "Die Gemeinde ist ein Pflichtfeld",
    "ADDRESS_MUNICIPALITY_TOO_SHORT": "Die Gemeinde ist zu kurz",
    "ADDRESS_NEIGHBORHOOD_REQUIRED": "Der Ortsteil ist ein Pflichtfeld",
    "ADDRESS_NEIGHBORHOOD_TOO_SHORT": "Der Ortsteil ist zu kurz",
    "ADDRESS_STATE_REQUIRED": "Der Bundesstaat ist ein Pflichtfeld",
    "ADDRESS_STATE_TOO_SHORT": "Der Bundesstaat ist zu kurz",
    "ADDRESS_STREET_REQUIRED": "Die Straße ist ein Pflichtfeld",
    "ADDRESS_STREET_TOO_SHORT": "Die Straße ist zu kurz",
    "ALREADY_EMPLOYEE": "Diese Person ist bereits Mitarbeiter Ihres Unternehmens",
    "BIRTH_DATE_INVALID": "Das Geburtsdatum ist ungültig",
    "BIRTH_DATE_NOT_PAST": "Das Geburtsdatum muss vor dem heutigen Tag liegen",
    "COMPANY_ACTIVATED": "Unternehmen aktiviert",
    "COMPANY_ADMIN_NOT_FOUND": "Administrator nicht gefunden",
    "COMPANY_DEACTIVATED": "Unternehmen deaktiviert",
    "COMPANY_DUPLICATE": "Die Steuernummer oder die E-Mail-Adresse ist bereits registriert",
    "COMPANY_HAS_BILLING": (
        "Für das Unternehmen sind Gebühren oder Zahlungen erfasst: Deaktivieren oder sperren Sie es, statt es zu "
        "löschen"
    ),
    "COMPANY_HAS_EMPLOYEES": "Das Unternehmen hat registrierte Mitarbeiter: Deaktivieren Sie es, statt es zu löschen",
    "COMPANY_NOT_FOUND": "Unternehmen nicht gefunden",
    "COMPANY_RFC_LENGTH": "Der RFC muss 12 Zeichen (juristische Person) oder 13 Zeichen (natürliche Person) haben",
    "COMPANY_TAX_ID_TAKEN": "Es gibt bereits ein Unternehmen mit dieser Steuernummer",
    "COORDINATES_INCOMPLETE": "Geben Sie Breiten- und Längengrad des Punkts auf der Karte an",
    "COUNTRY_INVALID": "Wählen Sie ein Land aus der Liste",
    "CURP_AVAILABLE": "CURP verfügbar",
    "CURP_BIRTH_DATE_MISMATCH": "Laut CURP ist das Geburtsdatum der {document}, angegeben ist jedoch der {birth}",
    "CURP_CENTURY_MISMATCH": (
        "Die CURP passt nicht zum Jahrhundert des Geburtsdatums: Das 17. Zeichen ist bei Geburt vor 2000 eine Ziffer "
        "und ab 2000 ein Buchstabe"
    ),
    "CURP_CHECK_DIGIT": "Die CURP ist ungültig: Die Prüfziffer stimmt nicht",
    "CURP_DATE_INVALID": "Das Datum in der CURP (JJMMTT) ist ungültig",
    "CURP_FORMAT": "Die CURP hat kein gültiges Format (z. B. HEGG560427MVZRRL04)",
    "CURP_LENGTH": {
        "one": "Die CURP hat {length} Zeichen; eingegeben wurde {count}",
        "other": "Die CURP hat {length} Zeichen; eingegeben wurden {count}",
    },
    "CURP_TAKEN": "Die CURP ist bereits bei einem anderen Mitarbeiter registriert",
    "DEPARTMENTS_LISTED": {
        "one": "{count} Abteilung",
        "other": "{count} Abteilungen",
    },
    "DEPARTMENT_CREATED": "Abteilung erstellt",
    "DEPARTMENT_DELETED": "Abteilung gelöscht",
    "DEPARTMENT_EMPLOYEE_ASSIGNED": "Mitarbeiter zugeordnet",
    "DEPARTMENT_EMPLOYEE_REMOVED": "Mitarbeiter aus der Abteilung entfernt",
    "DEPARTMENT_FOUND": "Abteilung gefunden",
    "DEPARTMENT_HAS_EMPLOYEES": (
        "Der Abteilung sind Mitarbeiter zugeordnet: Ordnen Sie sie neu zu oder entfernen Sie sie, bevor Sie die "
        "Abteilung löschen"
    ),
    "DEPARTMENT_MANAGER_ADDED": "Verantwortlicher hinzugefügt",
    "DEPARTMENT_MANAGER_REMOVED": "Verantwortlicher entfernt",
    "DEPARTMENT_NAME_TAKEN": "In Ihrem Unternehmen gibt es bereits eine Abteilung mit diesem Namen",
    "DEPARTMENT_NOT_FOUND": "Abteilung nicht gefunden",
    "DEPARTMENT_RESTORED": "Abteilung wiederhergestellt",
    "DEPARTMENT_UPDATED": "Abteilung aktualisiert",
    "DEVICES_LISTED": {
        "one": "{count} Gerät",
        "other": "{count} Geräte",
    },
    "DEVICE_STATUS_UPDATED": "Gerät aktualisiert",
    "DOCUMENT_OPTIONAL": "Optional: Das Feld kann leer bleiben",
    "EMAIL_AVAILABLE": "E-Mail-Adresse verfügbar",
    "EMAIL_INVALID": "Ungültige E-Mail-Adresse",
    "EMAIL_REQUIRED": "Die E-Mail-Adresse ist ein Pflichtfeld",
    "EMAIL_TAKEN": "Die E-Mail-Adresse ist auf der Plattform bereits registriert",
    "EMPLOYEES_GONE": "Einige Mitarbeiter existieren nicht mehr: Aktualisieren Sie die Liste",
    "EMPLOYEES_LISTED": {
        "one": "{count} Mitarbeiter gefunden",
        "other": "{count} Mitarbeiter gefunden",
    },
    "EMPLOYEES_ONLY": "Nur Mitarbeiter können diese Funktion verwenden",
    "EMPLOYEE_ACTIVATED": "Mitarbeiter aktiviert",
    "EMPLOYEE_CREATED": "Mitarbeiter registriert",
    "EMPLOYEE_DEACTIVATED": "Mitarbeiter deaktiviert",
    "EMPLOYEE_DELETED": "Mitarbeiter gelöscht",
    "EMPLOYEE_DEVICE_APPROVED": "Gerät freigegeben: Es gilt nicht mehr als unbekannt",
    "EMPLOYEE_DEVICE_REVOKED": "Freigabe widerrufen: Das Gerät gilt jetzt als unbekannt",
    "EMPLOYEE_DUPLICATE": "E-Mail-Adresse, Telefonnummer, Personalnummer, RFC, CURP oder NSS ist bereits registriert",
    "EMPLOYEE_FOUND": "Mitarbeiter gefunden",
    "EMPLOYEE_IDS": {
        "one": "{count} Mitarbeiter im Filter",
        "other": "{count} Mitarbeiter im Filter",
    },
    "EMPLOYEE_IDS_REPEATED": "Jeder Mitarbeiter darf nur einmal vorkommen",
    "EMPLOYEE_INACTIVE": "Der Mitarbeiter ist inaktiv",
    "EMPLOYEE_LIMIT_REACHED": (
        "Ihr Unternehmen hat sein Limit von {count} Mitarbeitern erreicht. Wenden Sie sich an den Administrator der "
        "Plattform."
    ),
    "EMPLOYEE_NOT_FOUND": "Mitarbeiter nicht gefunden",
    "EMPLOYEE_NUMBER_AVAILABLE": "Personalnummer verfügbar",
    "EMPLOYEE_NUMBER_INVALID": (
        "Die Personalnummer muss 1–30 Zeichen haben: Buchstaben, Ziffern, Bindestrich oder Unterstrich"
    ),
    "EMPLOYEE_NUMBER_TAKEN": "Die Personalnummer ist bereits registriert",
    "EMPLOYEE_RESTORED": "Mitarbeiter wiederhergestellt. Das Gesicht muss erneut registriert werden.",
    "EMPLOYEE_TOO_YOUNG": "Der Mitarbeiter muss mindestens {count} Jahre alt sein",
    "EMPLOYEE_UPDATED": "Mitarbeiter aktualisiert",
    "ENROLLMENT_RESET_BY_COMPANY": "Registrierung vom Unternehmen zurückgesetzt",
    "FACE_ERASED_ON_DELETE": (
        "Ihre Gesichtsdaten wurden beim Löschen Ihres Datensatzes entfernt. Registrieren Sie Ihr Gesicht erneut."
    ),
    "FACE_NOT_APPROVED": "Der Mitarbeiter hat noch kein freigegebenes Gesicht: Registrieren Sie es zuerst",
    "FACE_NOT_ENROLLED_YET": "Sie müssen zuerst Ihr Gesicht registrieren",
    "FACE_PENDING_REVIEW": "Ihre Gesichtsregistrierung wird von Ihrem Unternehmen geprüft",
    "FACE_REJECTED_ENROLL_AGAIN": "Ihre Gesichtsregistrierung wurde abgelehnt. Registrieren Sie Ihr Gesicht erneut.",
    "FIELD_NOT_ALLOWED": "Sie können dieses Feld nicht prüfen",
    "FIELD_REQUIRED": "Dieses Feld ist ein Pflichtfeld",
    "IDENTITY_REVERIFY_REQUESTED": "Der Mitarbeiter muss sein Gesicht erneut registrieren",
    "IDENTITY_REVERIFY_REQUESTED_ALL": {
        "one": "{count} Mitarbeiter muss sein Gesicht erneut registrieren",
        "other": "{count} Mitarbeiter müssen ihr Gesicht erneut registrieren",
    },
    "LAST_COMPANY_ADMIN": "Das Unternehmen muss mindestens einen aktiven Administrator behalten",
    "LEGAL_NAME_REQUIRED": "Der offizielle Firmenname ist ein Pflichtfeld",
    "LOCATION_POINT_REQUIRED": "Um einen Standort vorzuschreiben, markieren Sie den Punkt der Adresse auf der Karte",
    "LOCATION_RADIUS_REQUIRED": "Geben Sie in Metern den Radius an, in dem sich das Prüfgerät anmelden kann",
    "MAX_CHARACTERS": "Höchstens {count} Zeichen",
    "NAME_AVAILABLE": "Name verfügbar",
    "NAME_INVALID_CHARACTERS": "Zulässig sind nur Buchstaben, Leerzeichen, Apostrophe, Punkte und Bindestriche",
    "NAME_REQUIRED": "Der Name ist ein Pflichtfeld",
    "NAME_TOO_LONG": "Der Name darf höchstens {count} Zeichen haben",
    "NSS_AVAILABLE": "NSS verfügbar",
    "NSS_CHECK_DIGIT": "Die NSS ist ungültig: Die Prüfziffer stimmt nicht",
    "NSS_LENGTH": "Die NSS hat {count} Ziffern",
    "NSS_TAKEN": "Die NSS ist bereits bei einem anderen Mitarbeiter registriert",
    "PASSWORD_NEEDS_DIGIT": "Das Passwort muss mindestens eine Ziffer enthalten",
    "PASSWORD_NEEDS_LOWERCASE": "Das Passwort muss mindestens einen Kleinbuchstaben enthalten",
    "PASSWORD_NEEDS_UPPERCASE": "Das Passwort muss mindestens einen Großbuchstaben enthalten",
    "PASSWORD_REQUIRED": "Für eine neue Person ist ein Passwort erforderlich",
    "PASSWORD_TOO_LONG": "Das Passwort darf höchstens {count} Zeichen haben",
    "PASSWORD_TOO_SHORT": "Das Passwort muss mindestens {count} Zeichen haben",
    "PHONE_AVAILABLE": "Telefonnummer verfügbar",
    "PHONE_COUNTRY_UNAVAILABLE": "Telefonnummern aus diesem Land werden nicht akzeptiert",
    "PHONE_INVALID": "Die Telefonnummer ist ungültig",
    "PHONE_INVALID_FOR_CODE": "Die Telefonnummer ist für die Ländervorwahl +{code} ungültig",
    "PHONE_REQUIRED": "Die Telefonnummer ist ein Pflichtfeld",
    "PHONE_VALID": "Gültige Telefonnummer",
    "POSTAL_CODE_INVALID": "Die Postleitzahl ist ungültig",
    "POSTAL_CODE_MX": "Postleitzahlen in Mexiko haben 5 Ziffern",
    "QR_REVOKED": "QR-Code ungültig gemacht",
    "QR_SUMMARY": "Aktivität des QR-Codes",
    "RESTORE_CURP_TAKEN": "Wiederherstellen nicht möglich: Ein anderer Mitarbeiter hat bereits die CURP {value}",
    "RESTORE_EMPLOYEE_NUMBER_TAKEN": (
        "Wiederherstellen nicht möglich: Ein anderer Mitarbeiter hat bereits die Personalnummer {value}"
    ),
    "RESTORE_EMPLOYMENT_TAKEN": (
        "Wiederherstellen nicht möglich: Diese Person hat bereits einen anderen gültigen Datensatz in Ihrem Unternehmen"
    ),
    "RESTORE_NSS_TAKEN": "Wiederherstellen nicht möglich: Ein anderer Mitarbeiter hat bereits die NSS {value}",
    "RESTORE_RFC_TAKEN": "Wiederherstellen nicht möglich: Ein anderer Mitarbeiter hat bereits den RFC {value}",
    "REVERIFY_DEFAULT_REASON": "Ihr Unternehmen bittet Sie, Ihre Identität erneut zu bestätigen",
    "RFC_AVAILABLE": "RFC verfügbar",
    "RFC_BIRTH_DATE_MISMATCH": "Laut RFC ist das Geburtsdatum der {document}, angegeben ist jedoch der {birth}",
    "RFC_DATE_INVALID": "Das Datum im RFC (JJMMTT) ist ungültig",
    "RFC_FORMAT": "Der RFC hat kein gültiges Format (z. B. PEGJ900515AB1)",
    "RFC_GENERIC": "Der generische RFC ist ungültig. Geben Sie den RFC der Person ein.",
    "RFC_LENGTH": {
        "one": "Der RFC einer natürlichen Person hat {length} Zeichen; eingegeben wurde {count}",
        "other": "Der RFC einer natürlichen Person hat {length} Zeichen; eingegeben wurden {count}",
    },
    "RFC_TAKEN": "Der RFC ist bereits bei einem anderen Mitarbeiter registriert",
    "SHARED_ACCOUNT": (
        "Diese Person arbeitet auch für ein anderes Unternehmen. E-Mail-Adresse, Telefonnummer und Passwort der "
        "Person können hier nicht geändert werden."
    ),
    "TAX_ID_AVAILABLE": "Steuernummer verfügbar",
    "TAX_ID_CHECK_DIGIT": "Die Prüfziffer für {name} stimmt nicht. Prüfen Sie die Nummer.",
    "TAX_ID_FORMAT": "Ungültiges Format für {name} (z. B. {example})",
    "TAX_ID_LENGTH": "Die Nummer für {name} muss {min} bis {max} Zeichen haben",
    "TAX_ID_LENGTH_EXACT": "Die Nummer für {name} muss {length} Zeichen haben",
    "TAX_ID_TYPE_COUNTRY": "{name} ist keine Steuernummer für das Land {country}",
    "TAX_ID_TYPE_INVALID": "Wählen Sie eine Art der Kennung aus der Liste",
    "TRADE_NAME_REQUIRED": "Der Handelsname ist ein Pflichtfeld",
    "VALIDATION_COMPANY_REQUIRED": "Diese Prüfung ist nur für Unternehmenskonten möglich",
    "VALIDATOR_CREATED": "Prüfgerät registriert",
    "VALIDATOR_DELETED": "Prüfgerät gelöscht",
    "VALIDATOR_FOUND": "Prüfgerät gefunden",
    "VALIDATOR_NAME_REQUIRED": "Der Name des Prüfgeräts ist ein Pflichtfeld",
    "VALIDATOR_NOT_FOUND": "Prüfgerät nicht gefunden",
    "VALIDATOR_PASSWORD_RESET": "Passwort zurückgesetzt",
    "VALIDATOR_RESTORED": "Prüfgerät wiederhergestellt",
    "VALIDATOR_STATUS_UPDATED": "Prüfgerät aktualisiert",
    "VALIDATOR_UPDATED": "Prüfgerät aktualisiert",
    "VERIFICATIONS_LISTED": {
        "one": "{count} Prüfversuch",
        "other": "{count} Prüfversuche",
    },
}
