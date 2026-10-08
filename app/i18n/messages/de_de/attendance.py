"""Asistencia: turnos, sitios, registros del día, jornadas, calendario de días libres y ausencias.

Textos en alemán de Alemania (de-DE, trato de «Sie»): misma llave, mismos `{parámetros}` y mismas formas de plural
en todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ABSENCES": {
        "one": "{count} Abwesenheit",
        "other": "{count} Abwesenheiten",
    },
    "ABSENCES_CREATED": {
        "one": "Abwesenheit für {count} Mitarbeiter erfasst",
        "other": "Abwesenheit für {count} Mitarbeiter erfasst",
    },
    "ABSENCES_SUMMARY": {
        "one": "{count} offen",
        "other": "{count} offen",
    },
    "ABSENCE_APPROVED": "Antrag genehmigt",
    "ABSENCE_CANCELLED": "Abwesenheit storniert",
    "ABSENCE_IN_PAST": "Wählen Sie Daten ab heute",
    "ABSENCE_NOT_ACTIVE": "Die Abwesenheit ist nicht mehr gültig",
    "ABSENCE_NOT_FOUND": "Abwesenheit nicht gefunden",
    "ABSENCE_OVERLAP": "Überschneidet sich mit der Abwesenheit „{kind}“ ({state}) vom {start} bis {end}",
    "ABSENCE_REJECTED": "Antrag abgelehnt",
    "ABSENCE_REQUESTED": "Antrag an Ihr Unternehmen gesendet",
    "ABSENCE_REQUEST_CANCELLED": "Antrag zurückgezogen",
    "ABSENCE_TOO_LONG": "Eine Abwesenheit darf höchstens {count} Tage dauern",
    "ALREADY_CHECKED_IN": "Sie sind bereits eingestempelt",
    "ALREADY_ON_BREAK": "Sie sind bereits in der Pause",
    "ASSIGNMENT_ALREADY_SCHEDULED": "Ab dem {date} ist bereits ein Schichtwechsel geplant: Stornieren Sie ihn zuerst",
    "ASSIGNMENT_IN_PAST": "Die Schicht kann nicht an einem vergangenen Datum beginnen",
    "ASSIGNMENT_NOTICE_REQUIRED": (
        "Ein Schichtwechsel muss mindestens einen Tag im Voraus geplant werden: Wählen Sie ein Datum ab morgen"
    ),
    "ASSIGNMENT_NOT_FOUND": "Zuordnung nicht gefunden",
    "ASSIGNMENT_STARTED": (
        "Die Zuordnung hat bereits begonnen: Um die Schicht zu ändern, planen Sie ab morgen eine neue"
    ),
    "ATTENDANCE_BOARD": {
        "one": "{count} Mitarbeiter mit Schicht",
        "other": "{count} Mitarbeiter mit Schicht",
    },
    "ATTENDANCE_DONE_UNDER_REVIEW": "{done}: wird von Ihrem Unternehmen geprüft",
    "ATTENDANCE_INVALID_PERIOD": "Wählen Sie einen gültigen Zeitraum von höchstens {count} Tagen",
    "ATTENDANCE_REVIEWS": "Zeitbuchungen in Prüfung",
    "ATTENDANCE_REVIEW_CONFIRMED": "Zeitbuchung bestätigt",
    "ATTENDANCE_REVIEW_NOT_PENDING": "Dieser Arbeitstag ist nicht in Prüfung",
    "ATTENDANCE_REVIEW_REJECTED": "Zeitbuchung abgelehnt",
    "ATTENDANCE_SESSION_CORRECTED": "Anwesenheit korrigiert",
    "ATTENDANCE_SESSION_CREATED": "Anwesenheit erfasst",
    "ATTENDANCE_SESSION_EXISTS": (
        "Für diesen Tag ist bereits ein Arbeitstag erfasst: Korrigieren Sie ihn, statt einen weiteren zu erfassen"
    ),
    "ATTENDANCE_SESSION_OPEN": (
        "Es gibt noch einen offenen Arbeitstag (vom {date}): Erfassen Sie zuerst das Ausstempeln für diesen Tag"
    ),
    "BREAKS_EXCEEDED": {
        "one": "Die Schicht erlaubt {count} Pause",
        "other": "Die Schicht erlaubt {count} Pausen",
    },
    "BREAKS_FILL_SHIFT": "Die Pausen dürfen nicht die gesamte Schicht ausfüllen",
    "BREAKS_OVERLAP": "Die Pausen dürfen sich nicht überschneiden",
    "BREAKS_USED": "Sie haben die Pausen dieser Schicht bereits genommen",
    "BREAK_ENDED": "Pause beendet",
    "BREAK_INVALID": "Jede Pause muss nach ihrem Beginn enden",
    "BREAK_OUTSIDE_SESSION": "Pausen müssen zwischen Kommen und Gehen liegen",
    "BREAK_OUTSIDE_WORKING_HOURS": "Pausen müssen zwischen {start} und {end} beginnen",
    "BREAK_STARTED": "Pause begonnen",
    "BREAK_TOO_SHORT": "Jede Pause muss mindestens {count} Minuten dauern",
    "BREAK_WINDOW": "Sie können Ihre Pause zwischen {start} und {end} nehmen",
    "CHECK_IN_ALREADY_AT": "Bereits um {time} eingestempelt.",
    "CHECK_IN_OUTSIDE_SHIFT": "Das Einstempeln muss zwischen {opens} und {end} erfolgen",
    "CHECK_IN_RECORDED": "Eingestempelt",
    "CHECK_OUT_AFTER_DEADLINE": "Das Ausstempeln muss spätestens um {deadline} erfolgen",
    "CHECK_OUT_BEFORE_CHECK_IN": "Das Ausstempeln muss nach dem Einstempeln liegen",
    "CHECK_OUT_RECORDED": "Ausgestempelt",
    "DATE_RANGE_INVERTED": "Das Enddatum darf nicht vor dem Startdatum liegen",
    "DAY_OFF_FALLBACK_PHRASE": "Sie haben frei",
    "DAY_OFF_MANUAL": (
        "{reason}: Der {date} ist für {name} ein freier Tag. Wenn die Person gearbeitet hat, markieren Sie den Tag "
        "im Kalender als Arbeitstag und erfassen Sie dann die Anwesenheit."
    ),
    "DAY_OFF_ON": "{phrase} am {date}",
    "DAY_OFF_RANGE": "{phrase} vom {start} bis {end}",
    "DAY_OFF_TYPE_INVALID": "Wählen Sie eine gültige Abwesenheitsart",
    "DAY_OFF_TYPE_NOT_REQUESTABLE": (
        "„{name}“ wird von Ihrem Unternehmen erfasst: Wenden Sie sich direkt an Ihr Unternehmen"
    ),
    "DEVICE_KEY_INVALID": (
        "Der Schlüssel dieses Geräts ist ungültig. Laden Sie die Seite neu und versuchen Sie es erneut."
    ),
    "HOLIDAYS": {
        "one": "{count} Feiertag",
        "other": "{count} Feiertage",
    },
    "HOLIDAY_BENITO_JUAREZ": "Geburtstag von Benito Juárez",
    "HOLIDAY_CHRISTMAS": "Weihnachten",
    "HOLIDAY_CONSTITUTION": "Tag der Verfassung",
    "HOLIDAY_CREATED": "Feiertag hinzugefügt",
    "HOLIDAY_DATE_TAKEN": "An diesem Datum gibt es bereits einen Feiertag",
    "HOLIDAY_DELETED": "Feiertag gelöscht",
    "HOLIDAY_INDEPENDENCE": "Unabhängigkeitstag",
    "HOLIDAY_LABOR_DAY": "Tag der Arbeit",
    "HOLIDAY_NEW_YEAR": "Neujahr",
    "HOLIDAY_NOT_FOUND": "Feiertag nicht gefunden",
    "HOLIDAY_ON": "Der {date} ist ein Feiertag: {name}",
    "HOLIDAY_RESTORED": "Feiertag wiederhergestellt",
    "HOLIDAY_REVOLUTION": "Tag der Revolution",
    "HOLIDAY_TODAY": "Heute ist ein Feiertag: {name}",
    "HOLIDAY_TRANSMISSION": "Amtsübergabe der Präsidentschaft",
    "IMPOSSIBLE_TRAVEL": "Die Entfernung zu Ihrer letzten Zeitbuchung ist für die vergangene Zeit zu groß",
    "INVALID_REVIEW_DECISION": "Bestätigen Sie die Zeitbuchung oder lehnen Sie sie ab",
    "KIOSKS": {
        "one": "{count} Kiosk",
        "other": "{count} Kioske",
    },
    "KIOSK_CODE": "Standortcode",
    "KIOSK_CREATED": "Kiosk registriert",
    "KIOSK_DELETED": "Kiosk gelöscht",
    "KIOSK_NOT_FOUND": "Kiosk nicht gefunden",
    "KIOSK_PAIRED": "Kiosk gekoppelt",
    "KIOSK_PAIRING_INVALID": (
        "Der Kopplungscode ist ungültig oder abgelaufen. Fordern Sie bei Ihrem Unternehmen einen neuen an."
    ),
    "KIOSK_PAIRING_RENEWED": "Neuer Kopplungscode",
    "KIOSK_PROOF_INVALID": "Dieser Kiosk konnte nicht verifiziert werden. Versuchen Sie es erneut.",
    "KIOSK_PROOF_REQUIRED": "Dieser Kiosk muss noch verifiziert werden. Versuchen Sie es erneut.",
    "KIOSK_RESTORED": "Kiosk wiederhergestellt",
    "KIOSK_UNPAIRED": (
        "Dieser Kiosk ist nicht mehr gekoppelt. Fordern Sie bei Ihrem Unternehmen einen neuen Kopplungscode an."
    ),
    "LOCATION_NOT_PRECISE": (
        "Ihre Position ist ungenau (±{accuracy}; erforderlich: ±{required}). Aktivieren Sie die genaue "
        "Standortbestimmung oder GPS."
    ),
    "LOCATION_OUT_OF_SITE": "Heute müssen Sie an Ihrem Standort stempeln.",
    "LOCATION_OUT_OF_SITE_NEAREST": (
        "Heute müssen Sie an Ihrem Standort stempeln. Sie sind {distance} von {site} entfernt."
    ),
    "LOCATION_SAMPLES_INVALID": (
        "Die Positionsmessungen sind ungültig: Senden Sie höchstens {max}, jeweils mit Breitengrad, Längengrad und "
        "Genauigkeit."
    ),
    "MY_ABSENCES": {
        "one": "{count} Abwesenheit",
        "other": "{count} Abwesenheiten",
    },
    "MY_HOLIDAYS": {
        "one": "{count} Feiertag",
        "other": "{count} Feiertage",
    },
    "MY_WORK_SESSIONS": {
        "one": "{count} Arbeitstag",
        "other": "{count} Arbeitstage",
    },
    "NEXT_SHIFT": "Ihre nächste Schicht ist am {day} um {start}; Sie können ab {opens} einstempeln.",
    "NOT_CHECKED_IN": "Sie sind nicht eingestempelt",
    "NOT_RECORDED_REASON": "Nicht erfasst: {reason}.",
    "NO_BREAK_IN_PROGRESS": "Sie haben keine laufende Pause",
    "NO_CHECK_IN_FOR_CHECK_OUT": "Sie sind nicht eingestempelt und können daher nicht ausstempeln",
    "NO_SHIFT_ASSIGNED": "Ihnen ist keine Schicht zugeordnet: Wenden Sie sich an Ihr Unternehmen.",
    "NO_SHIFT_NOW": "Sie haben gerade keine Schicht",
    "NO_SHIFT_THAT_DAY": "{name} hat am {date} keine Schicht",
    "NO_SHIFT_TO_RECORD": "Derzeit gibt es keine Schicht zu erfassen.",
    "OFFICIAL_HOLIDAYS_ADDED": {
        "one": "{count} gesetzlicher Feiertag hinzugefügt",
        "other": "{count} gesetzliche Feiertage hinzugefügt",
    },
    "ON_BREAK_SINCE": "In der Pause seit {time}.",
    "ON_SHIFT_SINCE": "In der Schicht seit {since}; Sie stempeln um {until} aus.",
    "REASON_REQUIRED": "Erläutern Sie kurz den Grund",
    "RECORDED_AT": "{done} um {time}.",
    "REMOTE_DAY_OUTSIDE_SHIFT": "Die Schicht arbeitet nicht am {days}: Dieser Tag kann kein Telearbeitstag sein",
    "REQUEST_ALREADY_HANDLED": "Der Antrag wurde bereits bearbeitet",
    "RESTORE_ASSIGNMENT_CURRENT": (
        "Wiederherstellen nicht möglich: Der Mitarbeiter hat diese Schicht bereits ohne Enddatum"
    ),
    "RESTORE_EMPLOYEE_DELETED": (
        "Wiederherstellen nicht möglich: Der Mitarbeiter ist unter „Gelöscht“. Stellen Sie ihn zuerst wieder her."
    ),
    "RESTORE_HOLIDAY_DATE_TAKEN": "Wiederherstellen nicht möglich: Am {date} gibt es bereits einen Feiertag",
    "RESTORE_SHIFT_DELETED": (
        "Wiederherstellen nicht möglich: Die zugehörige Schicht ist unter „Gelöscht“. Stellen Sie sie zuerst wieder "
        "her."
    ),
    "RESTORE_SITES_DELETED": {
        "one": (
            "Wiederherstellen nicht möglich: Der Standort {sites} ist unter „Gelöscht“. Stellen Sie ihn zuerst wieder "
            "her."
        ),
        "other": (
            "Wiederherstellen nicht möglich: Die Standorte {sites} sind unter „Gelöscht“. Stellen Sie sie zuerst "
            "wieder her."
        ),
    },
    "RESTORE_WORKDAY_TAKEN": "Wiederherstellen nicht möglich: Dieser Tag ist bereits als Arbeitstag markiert",
    "REVIEW_NOTE_REQUIRED": "Begründen Sie die Ablehnung der Zeitbuchung: Der Mitarbeiter sieht die Begründung",
    "SHIFT": "Schicht",
    "SHIFTS": {
        "one": "{count} Schicht",
        "other": "{count} Schichten",
    },
    "SHIFT_ACTIVATED": "Schicht aktiviert",
    "SHIFT_ALREADY_RECORDED": "Sie haben diese Schicht bereits erfasst",
    "SHIFT_ALREADY_RECORDED_TODAY": "Sie haben diese Schicht bereits erfasst.",
    "SHIFT_ASSIGNED": "Schicht zugeordnet",
    "SHIFT_ASSIGNMENTS": {
        "one": "{count} Zuordnung",
        "other": "{count} Zuordnungen",
    },
    "SHIFT_ASSIGNMENT_CANCELLED": "Schichtwechsel storniert",
    "SHIFT_ASSIGNMENT_RESTORED": "Schichtwechsel wiederhergestellt",
    "SHIFT_BULK_ASSIGNED": {
        "one": "Schicht {count} Mitarbeiter zugeordnet",
        "other": "Schicht {count} Mitarbeitern zugeordnet",
    },
    "SHIFT_CHECK_IN_NOW": "Ihre Schicht geht von {start} bis {end}: Stempeln Sie jetzt ein.",
    "SHIFT_CREATED": "Schicht erstellt",
    "SHIFT_DEACTIVATED": "Schicht deaktiviert",
    "SHIFT_DELETED": "Schicht gelöscht",
    "SHIFT_ENDED_NO_CHECK_IN": "Ihre Schicht endete um {time}: Sie können nicht mehr einstempeln",
    "SHIFT_ENDED_UNRECORDED": "Ihre Schicht endete um {time}, ohne dass Sie eingestempelt haben.",
    "SHIFT_INACTIVE": "Die Schicht ist deaktiviert",
    "SHIFT_IN_USE": "Die Schicht ist Mitarbeitern zugeordnet: Deaktivieren Sie sie, statt sie zu löschen",
    "SHIFT_NAME_TAKEN": "Es gibt bereits eine Schicht mit diesem Namen",
    "SHIFT_NOT_AVAILABLE": "Diese Schicht ist nicht verfügbar",
    "SHIFT_NOT_FOUND": "Schicht nicht gefunden",
    "SHIFT_REQUESTS": {
        "one": "{count} Antrag",
        "other": "{count} Anträge",
    },
    "SHIFT_REQUESTS_SUMMARY": {
        "one": "{count} offen",
        "other": "{count} offen",
    },
    "SHIFT_REQUEST_APPROVED": "Schichtwechsel genehmigt",
    "SHIFT_REQUEST_CANCELLED": "Antrag zurückgezogen",
    "SHIFT_REQUEST_CLOSED": "Der Antrag wurde bereits bearbeitet",
    "SHIFT_REQUEST_CREATED": "Antrag an Ihr Unternehmen gesendet",
    "SHIFT_REQUEST_NOTICE_REQUIRED": (
        "Ein Schichtwechsel muss mindestens einen Tag im Voraus beantragt werden: Wählen Sie ein Datum ab morgen"
    ),
    "SHIFT_REQUEST_NOT_FOUND": "Antrag nicht gefunden",
    "SHIFT_REQUEST_PENDING": (
        "Sie haben bereits einen offenen Antrag auf Schichtwechsel: Warten Sie auf die Antwort oder ziehen Sie ihn "
        "zurück"
    ),
    "SHIFT_REQUEST_REJECTED": "Antrag abgelehnt",
    "SHIFT_RESTORED": "Schicht wiederhergestellt",
    "SHIFT_TIMES_EQUAL": "Das Schichtende muss sich vom Schichtbeginn unterscheiden",
    "SHIFT_UPDATED": "Schicht aktualisiert",
    "SHIFT_WINDOW_TOO_LONG": (
        "Frühes Einstempeln, Schicht und Frist zum Ausstempeln müssen zusammen weniger als 24 Stunden ergeben"
    ),
    "SITE": "Standort",
    "SITES": {
        "one": "{count} Standort",
        "other": "{count} Standorte",
    },
    "SITE_ACTIVATED": "Standort aktiviert",
    "SITE_CODE_DISABLED": "Der Code dieses Standorts ist ausgeschaltet. Bitten Sie Ihr Unternehmen, ihn zu aktivieren.",
    "SITE_CODE_INVALID": (
        "Der Standortcode ist ungültig oder hat sich geändert. Scannen Sie den aktuell angezeigten Code."
    ),
    "SITE_CODE_REQUIRED": "Zum Stempeln am Standort {site} scannen Sie den Code am Kiosk des Standorts.",
    "SITE_CODE_UNAVAILABLE": (
        "Der Standortcode konnte nicht erzeugt werden. Versuchen Sie es in einigen Minuten erneut."
    ),
    "SITE_CODE_USED": "Sie haben diesen Code bereits verwendet. Warten Sie auf den nächsten Code am Kiosk.",
    "SITE_CREATED": "Standort erstellt",
    "SITE_DEACTIVATED": "Standort deaktiviert",
    "SITE_DELETED": "Standort gelöscht",
    "SITE_HAS_RECORDS": (
        "Für den Standort gibt es Zeitbuchungen: Deaktivieren Sie ihn, statt ihn zu löschen, damit sein Verlauf "
        "erhalten bleibt"
    ),
    "SITE_IN_USE": {
        "one": (
            "Der Standort gehört zur Schicht {shifts}: Entfernen Sie ihn aus dieser Schicht oder deaktivieren Sie "
            "ihn, statt ihn zu löschen"
        ),
        "other": (
            "Der Standort gehört zu den Schichten {shifts}: Entfernen Sie ihn aus diesen Schichten oder deaktivieren "
            "Sie ihn, statt ihn zu löschen"
        ),
    },
    "SITE_NAME_TAKEN": "Es gibt bereits einen Standort mit diesem Namen",
    "SITE_NOT_AVAILABLE": "Wählen Sie aktive Standorte des Unternehmens",
    "SITE_NOT_FOUND": "Standort nicht gefunden",
    "SITE_POINT_REQUIRED": "Markieren Sie den Punkt des Standorts auf der Karte",
    "SITE_REQUIRED": "Wählen Sie mindestens einen Standort zum Stempeln an Tagen ohne Telearbeit",
    "SITE_RESTORED": "Standort wiederhergestellt",
    "SITE_UPDATED": "Standort aktualisiert",
    "TIME_IN_FUTURE": "Uhrzeiten dürfen nicht in der Zukunft liegen",
    "WEEKDAYS_INVALID": "Die Tage reichen von 0 (Montag) bis 6 (Sonntag)",
    "WORKDAYS": {
        "one": "{count} Arbeitstag",
        "other": "{count} Arbeitstage",
    },
    "WORKDAY_CREATED": "Arbeitstag erfasst",
    "WORKDAY_DELETED": "Arbeitstag gelöscht",
    "WORKDAY_IN_PAST": (
        "Ein vergangener Tag kann kein Arbeitstag mehr werden: Wählen Sie heute oder einen künftigen Tag"
    ),
    "WORKDAY_NOT_FOUND": "Arbeitstag nicht gefunden",
    "WORKDAY_NOT_NEEDED": (
        "Der {date} ist für {name} bereits ein Arbeitstag: Er ist weder Feiertag noch Teil einer Abwesenheit"
    ),
    "WORKDAY_RESTORED": "Arbeitstag wiederhergestellt",
    "WORKDAY_TAKEN": "Dieser Tag ist für den Mitarbeiter bereits ein Arbeitstag",
    "WORK_SESSION": "Arbeitstag",
    "WORK_SESSIONS": {
        "one": "{count} Arbeitstag",
        "other": "{count} Arbeitstage",
    },
    "WORK_SESSION_NOT_FOUND": "Arbeitstag nicht gefunden",
}
