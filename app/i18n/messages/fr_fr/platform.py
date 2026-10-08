"""Plataforma (ADMIN) e integraciones: empresas, política de verificación, errores, rendimiento, consumo, llaves de la
API y casos de fraude.

Textos en francés de Francia (fr-FR, registro «vous»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ACCOUNT_DELETED_LABEL": "Compte n° {id} (n'existe plus)",
    "ACCOUNT_LABEL": "{email} ({role})",
    "API_DEVICE_KEY_INVALID": "La clé de l'appareil n'est pas valide",
    "API_DEVICE_PROOF_INVALID": "Impossible de vérifier l'appareil. Demandez un nouveau défi.",
    "API_DEVICE_PROOF_REQUIRED": "La preuve de l'appareil est manquante : sa clé, le défi et la signature",
    "API_EMPLOYEE_REFERENCE_INVALID": "Envoyez l'identifiant ou le numéro de l'employé, un seul des deux",
    "API_KEYS_LISTED": {
        "one": "{count} clé",
        "other": "{count} clés",
    },
    "API_KEY_ACCESS_DISABLED": "L'entreprise de cette clé n'a pas accès à l'API",
    "API_KEY_COMPANY_INACTIVE": "L'entreprise de cette clé est désactivée",
    "API_KEY_COMPANY_SUSPENDED": "L'entreprise de cette clé est suspendue",
    "API_KEY_CREATED": "Clé créée",
    "API_KEY_EXPIRED": "Cette clé d'API a expiré",
    "API_KEY_INVALID": "La clé d'API n'est pas valide",
    "API_KEY_LIMIT": "Votre entreprise a déjà {count} clés non révoquées. Révoquez celles que vous n'utilisez pas.",
    "API_KEY_NAME_REQUIRED": "Saisissez un nom pour la clé",
    "API_KEY_NOT_FOUND": "Clé introuvable",
    "API_KEY_REQUIRED": "La clé d'API est manquante: envoyez-la dans l'en-tête X-API-Key",
    "API_KEY_REVOKED": "Cette clé d'API a été révoquée",
    "API_KEY_REVOKED_DONE": "Clé révoquée",
    "API_KEY_REVOKED_ROTATE": "Une clé révoquée ne peut pas être renouvelée: créez-en une nouvelle",
    "API_KEY_ROTATED": "Clé renouvelée",
    "API_KEY_VALIDATORS_DISABLED": "L'entreprise de cette clé n'a pas le module des validateurs",
    "API_SCOPE_INVALID": "Autorisations non valides: {scopes}",
    "API_SCOPE_REQUIRED": (
        "Cette clé n'a pas l'autorisation « {scope} ». Demandez-la à votre entreprise (Intégrations)."
    ),
    "ATTENDANCE_FEED": {
        "one": "{count} identification",
        "other": "{count} identifications",
    },
    "ATTENDANCE_LISTED": {
        "one": "{count} identification",
        "other": "{count} identifications",
    },
    "CASE_STATUS_FILTER_INVALID": "Choisissez l'un des statuts de cas disponibles",
    "CLIENT_ERROR_RECORDED": "Défaillance enregistrée",
    "COMPANIES_LISTED": {
        "one": "{count} entreprise trouvée",
        "other": "{count} entreprises trouvées",
    },
    "COMPANY_ADMINS_LISTED": {
        "one": "{count} administrateur",
        "other": "{count} administrateurs",
    },
    "COMPANY_ADMIN_CREATED": "Administrateur ajouté",
    "COMPANY_ADMIN_FOUND": "Administrateur trouvé",
    "COMPANY_ADMIN_PASSWORD_RESET": "Mot de passe réinitialisé",
    "COMPANY_ADMIN_STATUS_UPDATED": "Administrateur mis à jour",
    "COMPANY_CREATED": "Entreprise ajoutée",
    "COMPANY_DELETED": "Entreprise supprimée",
    "COMPANY_EMPLOYEES": {
        "one": "{count} employé",
        "other": "{count} employés",
    },
    "COMPANY_FOUND": "Entreprise trouvée",
    "COMPANY_RESTORED": "Entreprise restaurée",
    "COMPANY_UPDATED": "Entreprise mise à jour",
    "COMPANY_USAGE": "Consommation de l'entreprise",
    "DRIFT_COMPANIES": {
        "one": "{count} entreprise",
        "other": "{count} entreprises",
    },
    "DRIFT_COMPUTED": {
        "zero": "Dérive calculée : aucune tentative dans la fenêtre",
        "one": "Dérive calculée ({count} ligne)",
        "other": "Dérive calculée ({count} lignes)",
    },
    "DRIFT_DISABLED": "La surveillance de la dérive est désactivée dans la configuration du serveur",
    "DRIFT_SIGNALS": {
        "one": "{count} signal",
        "other": "{count} signaux",
    },
    "DRIFT_SUMMARY": "Résumé de la dérive des signaux",
    "ERRORS_RESOLVED": {
        "one": "{count} erreur résolue",
        "other": "{count} erreurs résolues",
    },
    "ERROR_FILTER_REQUIRED": "Filtrez par statut ou par gravité pour marquer des erreurs comme résolues",
    "ERROR_FILTER_RESOLVED": "Ces erreurs sont déjà résolues",
    "ERROR_OCCURRENCES_LISTED": {
        "one": "{count} occurrence",
        "other": "{count} occurrences",
    },
    "ERROR_REPORTS_LISTED": {
        "one": "{count} erreur",
        "other": "{count} erreurs",
    },
    "ERROR_REPORT_FOUND": "Erreur trouvée",
    "ERROR_REPORT_NOT_FOUND": "Erreur introuvable",
    "ERROR_STATUS_UPDATED": "Suivi mis à jour",
    "ERROR_SUMMARY": "Résumé des erreurs",
    "FACE_LEARNING_FORGOTTEN": {
        "zero": "Aucun échantillon appris: la comparaison se fait uniquement avec son enregistrement approuvé",
        "one": (
            "Apprentissage réinitialisé ({count} échantillon): la comparaison se fait uniquement avec son "
            "enregistrement approuvé"
        ),
        "other": (
            "Apprentissage réinitialisé ({count} échantillons): la comparaison se fait uniquement avec son "
            "enregistrement approuvé"
        ),
    },
    "FACE_LEARNING_SUMMARY": "Évolution de la reconnaissance faciale",
    "FRAUD_CASE": "Cas de fraude",
    "FRAUD_CASES": {
        "one": "{count} cas",
        "other": "{count} cas",
    },
    "FRAUD_CASES_COUNT": "Cas de fraude actifs",
    "FRAUD_CASE_DECIDED": "Cas mis à jour",
    "FRAUD_CASE_NOTE": "Note ajoutée",
    "FRAUD_CASE_NOT_FOUND": "Cas de fraude introuvable",
    "FRAUD_CASE_SAME_STATUS": "Le cas a déjà ce statut",
    "FRAUD_EVIDENCE": "Preuves",
    "FRAUD_EVIDENCE_NOT_FOUND": "Les preuves ne sont plus disponibles",
    "FRAUD_NOTE_REQUIRED": "Expliquez pourquoi vous confirmez ou écartez la fraude",
    "FRAUD_NOTE_TEXT_REQUIRED": "Saisissez la note",
    "INTEGRATION_COMPANY": "Entreprise de la clé",
    "INVALID_ANTISPOOF_LEVEL": "Choisissez l'un des niveaux de détection d'usurpation disponibles",
    "INVALID_CASE_STATUS": "Choisissez le nouveau statut du cas",
    "INVALID_CONFIDENCE_LEVEL": "Choisissez l'un des niveaux de confiance disponibles",
    "INVALID_CURSOR": "Le curseur n'est pas valide",
    "INVALID_DEVICE_MODE": "Choisissez l'un des modes d'appareil disponibles",
    "INVALID_FLASH_MODE": "Choisissez l'un des modes de flash disponibles",
    "INVALID_POLICY_PRESET": "Choisissez l'un des niveaux prédéfinis",
    "INVALID_RANGE": "La date de début ne peut pas être postérieure à la date de fin",
    "INVALID_RISK_ACTION": "Choisissez l'une des actions disponibles",
    "INVALID_RISK_FALLBACK": "Si le moteur échoue, choisissez autoriser, alerter ou demander une étape de plus",
    "INVALID_RISK_SIGNAL": "Ce signal de risque n'existe pas",
    "INVALID_SIGNAL_MODE": "Choisissez l'un des modes du signal",
    "INVALID_SINCE_UNTIL": "`since` doit être antérieur à `until`",
    "INVALID_VOICE_PROFILE": "Choisissez l'une des voix disponibles",
    "LIVENESS_MOVES_MIN": "La preuve de vie a besoin d'au moins deux mouvements de tête activés",
    "PERFORMANCE_METRICS": {
        "one": "{count} élément",
        "other": "{count} éléments",
    },
    "PERFORMANCE_OVERVIEW": "Performances de la plateforme",
    "PERFORMANCE_SERIES": "Série des performances",
    "PERFORMANCE_STATEMENTS": {
        "one": "{count} requête",
        "other": "{count} requêtes",
    },
    "PERFORMANCE_WEB_VITALS": {
        "one": "{count} écran",
        "other": "{count} écrans",
    },
    "PLATFORM_STATS": "Indicateurs de la plateforme",
    "POLICY": "Politique de vérification",
    "POLICY_CHANGED_SINCE": "La politique a changé après la demande: le changement doit être demandé à nouveau",
    "POLICY_CHANGES": {
        "one": "{count} changement",
        "other": "{count} changements",
    },
    "POLICY_CHANGE_APPROVED": "Changement approuvé: la politique l'applique désormais",
    "POLICY_CHANGE_CANCELLED": "Changement annulé",
    "POLICY_CHANGE_EXPIRED": "Expiré sans approbation",
    "POLICY_CHANGE_NOT_FOUND": "Changement de politique introuvable",
    "POLICY_CHANGE_NOT_PENDING": "Ce changement a déjà été tranché",
    "POLICY_CHANGE_PENDING": (
        "Le changement réduit la sécurité: il s'appliquera lorsqu'un autre administrateur l'aura approuvé"
    ),
    "POLICY_CHANGE_REJECTED": "Changement refusé",
    "POLICY_NOT_REQUESTER": "Seule la personne qui a demandé le changement peut l'annuler",
    "POLICY_SELF_APPROVAL": (
        "Vous ne pouvez pas approuver votre propre changement: un autre administrateur doit le faire"
    ),
    "POLICY_SELF_DECISION": "Vous ne pouvez pas refuser votre propre changement: utilisez « Retirer »",
    "POLICY_SIMULATED": {
        "one": "{count} tentative simulée",
        "other": "{count} tentatives simulées",
    },
    "POLICY_UNCHANGED": "Aucun changement dans la politique",
    "POLICY_UPDATED": "Politique de vérification mise à jour",
    "RANGE_TOO_LONG": "Choisissez une plage d'un an au maximum",
    "RESTORE_COMPANY_TAX_ID_TAKEN": (
        "Restauration impossible: une autre entreprise a déjà cet identifiant fiscal ({name} {value})"
    ),
    "RISK_SCORES_ORDER": "Les seuils de risque doivent être dans l'ordre: moyen < élevé < critique",
    "SERVER_STATUS": "État du serveur",
    "SIGNAL_MEASURE_ONLY": "Ce signal est seulement mesuré: il ne peut pas être exigé tant qu'il n'est pas calibré",
    "SLOW_ALERTS_LISTED": {
        "one": "{count} alerte",
        "other": "{count} alertes",
    },
    "SLOW_ALERTS_SUMMARY": "Résumé des alertes",
    "SLOW_ALERT_FOUND": "Alerte trouvée",
    "SLOW_ALERT_NOT_FOUND": "Alerte introuvable",
    "SLOW_ALERT_STATUS_UPDATED": "Suivi mis à jour",
    "STORAGE_CLIENT_FAILED": "impossible de se connecter au stockage de Google",
    "STORAGE_COMPANY_DOCUMENTS": "Documents des entreprises",
    "STORAGE_EMPLOYEE_DOCUMENTS": "Documents d’identité des employés",
    "STORAGE_ENROLLMENT_VOICE_CLIPS": "Vidéos de la vérification vocale de l'enregistrement du visage",
    "STORAGE_FACE_ENROLLMENT_DRAFT_PHOTOS": "Photos initiales de l'enregistrement du visage (brouillons)",
    "STORAGE_FACE_ENROLLMENT_PHOTOS": "Photos de référence de l'enregistrement du visage",
    "STORAGE_FRAUD_EVIDENCE": "Images de preuve des cas de fraude",
    "STORAGE_KEY_INVALID": "la clé du compte de service n'est pas valide",
    "STORAGE_KEY_NOT_MOUNTED": "la clé du compte de service n'est pas encore montée (fichier vide)",
    "STORAGE_KEY_UNREADABLE": "impossible de lire la clé du compte de service ({error})",
    "STORAGE_NOT_CONFIGURED": "il manque GCS_BUCKET ou GCS_CREDENTIALS_FILE dans la configuration",
    "STORAGE_PAYMENT_RECEIPTS": "Justificatifs de paiement",
    "STORAGE_PENDING_DELETIONS": "Objets à supprimer du stockage",
    "STORAGE_USER_AVATARS": "Photos de profil (un objet par taille)",
    "THRESHOLDS_RECALIBRATED": {
        "zero": "Seuils recalculés: aucun changement",
        "one": "Seuils recalculés ({count} a changé)",
        "other": "Seuils recalculés ({count} ont changé)",
    },
    "TOO_MANY_SAMPLES": "Un lot accepte jusqu'à {count} échantillons",
    "USAGE_COMPANIES": {
        "one": "{count} entreprise",
        "other": "{count} entreprises",
    },
    "USAGE_OVERVIEW": "Consommation de la plateforme",
    "USAGE_ROUTES": {
        "one": "{count} route",
        "other": "{count} routes",
    },
    "USAGE_USERS": {
        "one": "{count} compte",
        "other": "{count} comptes",
    },
    "VALIDATORS_DISABLED": (
        "Votre entreprise n'a pas le module des validateurs. Demandez-le à l'administrateur de la plateforme."
    ),
    "VALIDATORS_LISTED": {
        "one": "{count} validateur",
        "other": "{count} validateurs",
    },
    "VALIDATOR_LIMIT_BELOW_ACTIVE": {
        "one": (
            "L'entreprise a {count} validateur actif: la limite ne peut pas être inférieure. Demandez-lui de le "
            "désactiver d'abord."
        ),
        "other": (
            "L'entreprise a {count} validateurs actifs: la limite ne peut pas être inférieure. Demandez-lui de "
            "désactiver ceux en trop."
        ),
    },
    "VALIDATOR_LIMIT_REACHED": {
        "zero": (
            "Votre entreprise n'a plus de place pour des validateurs. Demandez-en davantage à l'administrateur de la "
            "plateforme."
        ),
        "one": (
            "Votre entreprise a atteint sa limite de {count} validateur actif. Désactivez-en un ou demandez-en "
            "davantage à l'administrateur de la plateforme."
        ),
        "other": (
            "Votre entreprise a atteint sa limite de {count} validateurs actifs. Désactivez-en un ou demandez-en "
            "davantage à l'administrateur de la plateforme."
        ),
    },
    "WEB_PERFORMANCE_RECORDED": "Performances enregistrées",
}
