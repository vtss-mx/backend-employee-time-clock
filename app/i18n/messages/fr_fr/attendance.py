"""Asistencia: turnos, sitios, registros del día, jornadas, calendario de días libres y ausencias.

Textos en francés de Francia (fr-FR, registro «vous»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ABSENCES": {
        "one": "{count} absence",
        "other": "{count} absences",
    },
    "ABSENCES_CREATED": {
        "one": "Absence enregistrée pour {count} employé",
        "other": "Absence enregistrée pour {count} employés",
    },
    "ABSENCES_SUMMARY": {
        "one": "{count} en attente",
        "other": "{count} en attente",
    },
    "ABSENCE_APPROVED": "Demande approuvée",
    "ABSENCE_CANCELLED": "Absence annulée",
    "ABSENCE_IN_PAST": "Choisissez des dates à partir d'aujourd'hui",
    "ABSENCE_NOT_ACTIVE": "L'absence n'est plus en vigueur",
    "ABSENCE_NOT_FOUND": "Absence introuvable",
    "ABSENCE_OVERLAP": "Cela chevauche son absence « {kind} » ({state}) du {start} au {end}",
    "ABSENCE_REJECTED": "Demande refusée",
    "ABSENCE_REQUESTED": "Demande envoyée à votre entreprise",
    "ABSENCE_REQUEST_CANCELLED": "Demande annulée",
    "ABSENCE_TOO_LONG": "Une absence peut durer jusqu'à {count} jours",
    "ALREADY_CHECKED_IN": "Vous avez déjà pointé votre entrée",
    "ALREADY_ON_BREAK": "Vous êtes déjà en pause",
    "ASSIGNMENT_ALREADY_SCHEDULED": "Un changement d'horaire est déjà programmé à partir du {date}: annulez-le d'abord",
    "ASSIGNMENT_IN_PAST": "L'horaire ne peut pas commencer à une date passée",
    "ASSIGNMENT_NOTICE_REQUIRED": (
        "Un changement d'horaire se programme au moins un jour à l'avance: choisissez une date à partir de demain"
    ),
    "ASSIGNMENT_NOT_FOUND": "Affectation introuvable",
    "ASSIGNMENT_STARTED": (
        "L'affectation a déjà commencé: pour changer d'horaire, programmez-en un nouveau à partir de demain"
    ),
    "ATTENDANCE_BOARD": {
        "one": "{count} employé avec un horaire",
        "other": "{count} employés avec un horaire",
    },
    "ATTENDANCE_DONE_UNDER_REVIEW": "{done}: en attente de vérification par votre entreprise",
    "ATTENDANCE_INVALID_PERIOD": "Choisissez une plage de dates valide de {count} jours au maximum",
    "ATTENDANCE_REVIEWS": "Pointages en vérification",
    "ATTENDANCE_REVIEW_CONFIRMED": "Pointage confirmé",
    "ATTENDANCE_REVIEW_NOT_PENDING": "Cette journée de travail n'est pas en vérification",
    "ATTENDANCE_REVIEW_REJECTED": "Pointage refusé",
    "ATTENDANCE_SESSION_CORRECTED": "Présence corrigée",
    "ATTENDANCE_SESSION_CREATED": "Présence enregistrée",
    "ATTENDANCE_SESSION_EXISTS": (
        "Ce jour a déjà sa journée de travail enregistrée: corrigez-la au lieu d'en enregistrer une autre"
    ),
    "ATTENDANCE_SESSION_OPEN": (
        "Une autre journée de travail est encore ouverte (du {date}): enregistrez d'abord sa sortie"
    ),
    "BREAKS_EXCEEDED": {
        "one": "L'horaire permet {count} pause",
        "other": "L'horaire permet {count} pauses",
    },
    "BREAKS_FILL_SHIFT": "Les pauses ne peuvent pas couvrir tout l'horaire",
    "BREAKS_OVERLAP": "Les pauses ne peuvent pas se chevaucher",
    "BREAKS_USED": "Vous avez déjà pris les pauses de cet horaire",
    "BREAK_ENDED": "Pause terminée",
    "BREAK_INVALID": "Chaque pause doit se terminer après son début",
    "BREAK_OUTSIDE_SESSION": "Les pauses doivent se situer entre l'entrée et la sortie",
    "BREAK_OUTSIDE_WORKING_HOURS": "Les pauses doivent commencer entre {start} et {end}",
    "BREAK_STARTED": "Pause commencée",
    "BREAK_TOO_SHORT": "Chaque pause doit durer au moins {count} minutes",
    "BREAK_WINDOW": "Vous pouvez prendre votre pause entre {start} et {end}",
    "CHECK_IN_ALREADY_AT": "Entrée déjà pointée à {time}.",
    "CHECK_IN_OUTSIDE_SHIFT": "L'entrée doit se faire entre {opens} et {end}",
    "CHECK_IN_RECORDED": "Entrée enregistrée",
    "CHECK_OUT_AFTER_DEADLINE": "La sortie doit se faire au plus tard à {deadline}",
    "CHECK_OUT_BEFORE_CHECK_IN": "La sortie doit être postérieure à l'entrée",
    "CHECK_OUT_RECORDED": "Sortie enregistrée",
    "DATE_RANGE_INVERTED": "La date de fin ne peut pas être antérieure à la date de début",
    "DAY_OFF_FALLBACK_PHRASE": "Vous êtes en repos",
    "DAY_OFF_MANUAL": (
        "{reason}: le {date} est un jour de repos pour {name}. Si cette personne a travaillé, marquez ce jour comme "
        "ouvré dans Calendrier et enregistrez sa présence."
    ),
    "DAY_OFF_ON": "{phrase} le {date}",
    "DAY_OFF_RANGE": "{phrase} du {start} au {end}",
    "DAY_OFF_TYPE_INVALID": "Choisissez un type d'absence valide",
    "DAY_OFF_TYPE_NOT_REQUESTABLE": (
        "Ce type d'absence (« {name} ») est enregistré par votre entreprise: demandez-le-lui directement"
    ),
    "DEVICE_KEY_INVALID": "La clé de cet appareil n'est pas valide. Rechargez la page et réessayez.",
    "HOLIDAYS": {
        "one": "{count} jour férié",
        "other": "{count} jours fériés",
    },
    "HOLIDAY_BENITO_JUAREZ": "Anniversaire de Benito Juárez",
    "HOLIDAY_CHRISTMAS": "Noël",
    "HOLIDAY_CONSTITUTION": "Jour de la Constitution",
    "HOLIDAY_CREATED": "Jour férié ajouté",
    "HOLIDAY_DATE_TAKEN": "Il y a déjà un jour férié à cette date",
    "HOLIDAY_DELETED": "Jour férié supprimé",
    "HOLIDAY_INDEPENDENCE": "Jour de l'Indépendance",
    "HOLIDAY_LABOR_DAY": "Fête du Travail",
    "HOLIDAY_NEW_YEAR": "Jour de l'An",
    "HOLIDAY_NOT_FOUND": "Jour férié introuvable",
    "HOLIDAY_ON": "Le {date} est un jour férié: {name}",
    "HOLIDAY_RESTORED": "Jour férié restauré",
    "HOLIDAY_REVOLUTION": "Jour de la Révolution",
    "HOLIDAY_TODAY": "Aujourd'hui est un jour férié: {name}",
    "HOLIDAY_TRANSMISSION": "Passation du pouvoir exécutif fédéral",
    "IMPOSSIBLE_TRAVEL": "Vous êtes trop loin de votre pointage précédent pour le temps écoulé",
    "INVALID_REVIEW_DECISION": "Confirmez ou refusez le pointage",
    "KIOSKS": {
        "one": "{count} borne",
        "other": "{count} bornes",
    },
    "KIOSK_CODE": "Code du site",
    "KIOSK_CREATED": "Borne ajoutée",
    "KIOSK_DELETED": "Borne supprimée",
    "KIOSK_NOT_FOUND": "Borne introuvable",
    "KIOSK_PAIRED": "Borne associée",
    "KIOSK_PAIRING_INVALID": (
        "Le code d'association n'est pas valide ou a expiré. Demandez-en un nouveau à votre entreprise."
    ),
    "KIOSK_PAIRING_RENEWED": "Nouveau code d'association",
    "KIOSK_PROOF_INVALID": "Impossible de vérifier cette borne. Réessayez.",
    "KIOSK_PROOF_REQUIRED": "Cette borne doit encore être vérifiée. Réessayez.",
    "KIOSK_RESTORED": "Borne restaurée",
    "KIOSK_UNPAIRED": "Cette borne n'est plus associée. Demandez un nouveau code d'association à votre entreprise.",
    "LOCATION_NOT_PRECISE": (
        "Votre position n'est pas assez précise (±{accuracy}; ±{required} requis). Activez la localisation précise ou "
        "le GPS."
    ),
    "LOCATION_OUT_OF_SITE": "Aujourd'hui, vous devez pointer sur votre site de travail.",
    "LOCATION_OUT_OF_SITE_NEAREST": (
        "Aujourd'hui, vous devez pointer sur votre site de travail. Vous êtes à {distance} de {site}."
    ),
    "LOCATION_SAMPLES_INVALID": (
        "Les relevés de position ne sont pas valides: envoyez-en {max} au maximum, chacun avec latitude, longitude et "
        "précision."
    ),
    "MY_ABSENCES": {
        "one": "{count} absence",
        "other": "{count} absences",
    },
    "MY_HOLIDAYS": {
        "one": "{count} jour férié",
        "other": "{count} jours fériés",
    },
    "MY_WORK_SESSIONS": {
        "one": "{count} journée de travail",
        "other": "{count} journées de travail",
    },
    "NEXT_SHIFT": (
        "Votre prochaine journée de travail commence le {day} à {start}; vous pouvez pointer à partir de {opens}."
    ),
    "NOT_CHECKED_IN": "Vous n'avez pas pointé votre entrée",
    "NOT_RECORDED_REASON": "Non enregistré: {reason}.",
    "NO_BREAK_IN_PROGRESS": "Vous n'avez pas de pause en cours",
    "NO_CHECK_IN_FOR_CHECK_OUT": "Vous n'avez pas pointé votre entrée: impossible de pointer votre sortie",
    "NO_SHIFT_ASSIGNED": "Vous n'avez pas d'horaire attribué: demandez-en un à votre entreprise.",
    "NO_SHIFT_NOW": "Vous n'avez pas d'horaire en ce moment",
    "NO_SHIFT_THAT_DAY": "{name} n'a pas d'horaire le {date}",
    "NO_SHIFT_TO_RECORD": "Aucun horaire à enregistrer en ce moment.",
    "OFFICIAL_HOLIDAYS_ADDED": {
        "one": "{count} jour férié officiel ajouté",
        "other": "{count} jours fériés officiels ajoutés",
    },
    "ON_BREAK_SINCE": "En pause depuis {time}.",
    "ON_SHIFT_SINCE": "En poste depuis {since}; votre sortie est à {until}.",
    "REASON_REQUIRED": "Expliquez brièvement le motif",
    "RECORDED_AT": "{done} à {time}.",
    "REMOTE_DAY_OUTSIDE_SHIFT": (
        "Le télétravail n'est possible que les jours prévus par l'horaire, qui n'inclut pas {days}"
    ),
    "REQUEST_ALREADY_HANDLED": "La demande a déjà été traitée",
    "RESTORE_ASSIGNMENT_CURRENT": "Restauration impossible: l'employé a déjà cet horaire sans date de fin",
    "RESTORE_EMPLOYEE_DELETED": "Restauration impossible: l'employé est dans « Supprimés ». Restaurez-le d'abord.",
    "RESTORE_HOLIDAY_DATE_TAKEN": "Restauration impossible: il y a déjà un jour férié le {date}",
    "RESTORE_SHIFT_DELETED": "Restauration impossible: son horaire est dans « Supprimés ». Restaurez-le d'abord.",
    "RESTORE_SITES_DELETED": {
        "one": "Restauration impossible: le site {sites} est dans « Supprimés ». Restaurez-le d'abord.",
        "other": "Restauration impossible: les sites {sites} sont dans « Supprimés ». Restaurez-les d'abord.",
    },
    "RESTORE_WORKDAY_TAKEN": "Restauration impossible: ce jour est déjà marqué comme ouvré",
    "REVIEW_NOTE_REQUIRED": "Expliquez pourquoi vous refusez le pointage: l'employé le verra",
    "SHIFT": "Horaire",
    "SHIFTS": {
        "one": "{count} horaire",
        "other": "{count} horaires",
    },
    "SHIFT_ACTIVATED": "Horaire activé",
    "SHIFT_ALREADY_RECORDED": "Vous avez déjà pointé pour cet horaire",
    "SHIFT_ALREADY_RECORDED_TODAY": "Vous avez déjà pointé pour cet horaire.",
    "SHIFT_ASSIGNED": "Horaire attribué",
    "SHIFT_ASSIGNMENTS": {
        "one": "{count} affectation",
        "other": "{count} affectations",
    },
    "SHIFT_ASSIGNMENT_CANCELLED": "Changement d'horaire annulé",
    "SHIFT_ASSIGNMENT_RESTORED": "Changement d'horaire restauré",
    "SHIFT_BULK_ASSIGNED": {
        "one": "Horaire attribué à {count} employé",
        "other": "Horaire attribué à {count} employés",
    },
    "SHIFT_CHECK_IN_NOW": "Votre horaire est de {start} à {end}: pointez votre entrée.",
    "SHIFT_CREATED": "Horaire créé",
    "SHIFT_DEACTIVATED": "Horaire désactivé",
    "SHIFT_DELETED": "Horaire supprimé",
    "SHIFT_ENDED_NO_CHECK_IN": "Votre horaire s'est terminé à {time}: vous ne pouvez plus pointer votre entrée",
    "SHIFT_ENDED_UNRECORDED": "Votre horaire s'est terminé à {time} sans pointage d'entrée.",
    "SHIFT_INACTIVE": "L'horaire est désactivé",
    "SHIFT_IN_USE": "L'horaire est attribué à des employés: désactivez-le au lieu de le supprimer",
    "SHIFT_NAME_TAKEN": "Un horaire portant ce nom existe déjà",
    "SHIFT_NOT_AVAILABLE": "Cet horaire n'est pas disponible",
    "SHIFT_NOT_FOUND": "Horaire introuvable",
    "SHIFT_REQUESTS": {
        "one": "{count} demande",
        "other": "{count} demandes",
    },
    "SHIFT_REQUESTS_SUMMARY": {
        "one": "{count} en attente",
        "other": "{count} en attente",
    },
    "SHIFT_REQUEST_APPROVED": "Changement d'horaire approuvé",
    "SHIFT_REQUEST_CANCELLED": "Demande annulée",
    "SHIFT_REQUEST_CLOSED": "La demande a déjà été traitée",
    "SHIFT_REQUEST_CREATED": "Demande envoyée à votre entreprise",
    "SHIFT_REQUEST_NOTICE_REQUIRED": (
        "Le changement d'horaire se demande au moins un jour à l'avance: choisissez une date à partir de demain"
    ),
    "SHIFT_REQUEST_NOT_FOUND": "Demande introuvable",
    "SHIFT_REQUEST_PENDING": (
        "Vous avez déjà une demande de changement d'horaire en attente: attendez la réponse ou annulez-la"
    ),
    "SHIFT_REQUEST_REJECTED": "Demande refusée",
    "SHIFT_RESTORED": "Horaire restauré",
    "SHIFT_TIMES_EQUAL": "L'heure de sortie doit être différente de l'heure d'entrée",
    "SHIFT_UPDATED": "Horaire mis à jour",
    "SHIFT_WINDOW_TOO_LONG": (
        "L'entrée anticipée, l'horaire et la limite de sortie doivent totaliser moins de 24 heures"
    ),
    "SITE": "Site",
    "SITES": {
        "one": "{count} site",
        "other": "{count} sites",
    },
    "SITE_ACTIVATED": "Site activé",
    "SITE_CODE_DISABLED": "Le code de ce site est désactivé. Demandez à votre entreprise de l'activer.",
    "SITE_CODE_INVALID": "Le code du site n'est pas valide ou a changé. Scannez le code affiché maintenant.",
    "SITE_CODE_REQUIRED": "Pour pointer à {site}, scannez le code de la borne du site.",
    "SITE_CODE_UNAVAILABLE": "Impossible de générer le code du site. Réessayez dans quelques minutes.",
    "SITE_CODE_USED": "Vous avez déjà utilisé ce code. Attendez le suivant sur la borne.",
    "SITE_CREATED": "Site ajouté",
    "SITE_DEACTIVATED": "Site désactivé",
    "SITE_DELETED": "Site supprimé",
    "SITE_HAS_RECORDS": "Le site a des pointages: désactivez-le au lieu de le supprimer pour conserver son historique",
    "SITE_IN_USE": {
        "one": (
            "Le site fait partie de l'horaire {shifts}: retirez-le de cet horaire ou désactivez-le au lieu de le "
            "supprimer"
        ),
        "other": (
            "Le site fait partie des horaires {shifts}: retirez-le de ces horaires ou désactivez-le au lieu de le "
            "supprimer"
        ),
    },
    "SITE_NAME_TAKEN": "Un site portant ce nom existe déjà",
    "SITE_NOT_AVAILABLE": "Choisissez des sites actifs de l'entreprise",
    "SITE_NOT_FOUND": "Site introuvable",
    "SITE_POINT_REQUIRED": "Marquez sur la carte le point du site",
    "SITE_REQUIRED": "Choisissez au moins un site pour pointer les jours qui ne sont pas en télétravail",
    "SITE_RESTORED": "Site restauré",
    "SITE_UPDATED": "Site mis à jour",
    "TIME_IN_FUTURE": "Les heures ne peuvent pas être dans le futur",
    "WEEKDAYS_INVALID": "Les jours vont de 0 (lundi) à 6 (dimanche)",
    "WORKDAYS": {
        "one": "{count} jour ouvré",
        "other": "{count} jours ouvrés",
    },
    "WORKDAY_CREATED": "Jour ouvré ajouté",
    "WORKDAY_DELETED": "Jour ouvré supprimé",
    "WORKDAY_IN_PAST": "Un jour passé ne peut pas devenir ouvré: choisissez aujourd'hui ou un jour futur",
    "WORKDAY_NOT_FOUND": "Jour ouvré introuvable",
    "WORKDAY_NOT_NEEDED": (
        "Le {date} est déjà un jour ouvré pour {name}: ce n'est ni un jour férié ni un jour de son absence"
    ),
    "WORKDAY_RESTORED": "Jour ouvré restauré",
    "WORKDAY_TAKEN": "Ce jour est déjà ouvré pour l'employé",
    "WORK_SESSION": "Journée de travail",
    "WORK_SESSIONS": {
        "one": "{count} journée de travail",
        "other": "{count} journées de travail",
    },
    "WORK_SESSION_NOT_FOUND": "Journée de travail introuvable",
}
