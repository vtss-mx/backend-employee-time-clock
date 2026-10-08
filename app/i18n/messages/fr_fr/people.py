"""Personal y empresas: empleados, cuentas compartidas, departamentos, validadores, documentos (RFC, CURP, NSS),
teléfonos y domicilios.

Textos en francés de Francia (fr-FR, registro «vous»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ACCOUNT_EMAIL_TAKEN": "Cette adresse e-mail est déjà enregistrée",
    "ACCOUNT_LINKABLE_EMAIL": (
        "Cette personne a déjà un compte sur la plateforme. Elle sera ajoutée à votre entreprise avec son mot de passe "
        "actuel."
    ),
    "ACCOUNT_LINKABLE_PHONE": (
        "Ce téléphone appartient à un compte existant. Utilisez l'adresse e-mail de cette personne."
    ),
    "ACCOUNT_PHONE_MATCHES": (
        "Le téléphone correspond au compte de cette personne. Elle sera associée à votre entreprise."
    ),
    "ACCOUNT_PHONE_MISMATCH": (
        "Cette adresse e-mail a déjà un compte avec un autre téléphone. Vérifiez le téléphone de la personne."
    ),
    "ACCOUNT_PHONE_MISSING": (
        "Le compte de cette personne n'a pas de téléphone. Son entreprise actuelle doit l'ajouter avant de pouvoir "
        "l'associer."
    ),
    "ACCOUNT_PHONE_TAKEN": "Ce téléphone est déjà enregistré sur un autre compte",
    "ADDRESS_CITY_REQUIRED": "La ville est obligatoire",
    "ADDRESS_CITY_TOO_SHORT": "Le nom de la ville est trop court",
    "ADDRESS_EXTERIOR_NUMBER_REQUIRED": "Le numéro de rue est obligatoire",
    "ADDRESS_MUNICIPALITY_REQUIRED": "La municipalité est obligatoire",
    "ADDRESS_MUNICIPALITY_TOO_SHORT": "Le nom de la municipalité est trop court",
    "ADDRESS_NEIGHBORHOOD_REQUIRED": "Le quartier est obligatoire",
    "ADDRESS_NEIGHBORHOOD_TOO_SHORT": "Le nom du quartier est trop court",
    "ADDRESS_STATE_REQUIRED": "L'État est obligatoire",
    "ADDRESS_STATE_TOO_SHORT": "Le nom de l'État est trop court",
    "ADDRESS_STREET_REQUIRED": "La rue est obligatoire",
    "ADDRESS_STREET_TOO_SHORT": "Le nom de la rue est trop court",
    "ALREADY_EMPLOYEE": "Cette personne fait déjà partie du personnel de votre entreprise",
    "BIRTH_DATE_INVALID": "La date de naissance n'est pas valide",
    "BIRTH_DATE_NOT_PAST": "La date de naissance doit être antérieure à aujourd'hui",
    "COMPANY_ACTIVATED": "Entreprise activée",
    "COMPANY_ADMIN_NOT_FOUND": "Administrateur introuvable",
    "COMPANY_DEACTIVATED": "Entreprise désactivée",
    "COMPANY_DUPLICATE": "L'identifiant fiscal ou l'adresse e-mail est déjà enregistré",
    "COMPANY_HAS_BILLING": (
        "L'entreprise a des échéances ou des paiements enregistrés: désactivez-la ou suspendez-la au lieu de la "
        "supprimer"
    ),
    "COMPANY_HAS_EMPLOYEES": "L'entreprise a des employés enregistrés: désactivez-la au lieu de la supprimer",
    "COMPANY_NOT_FOUND": "Entreprise introuvable",
    "COMPANY_RFC_LENGTH": "Le RFC doit comporter 12 caractères (personne morale) ou 13 (personne physique)",
    "COMPANY_TAX_ID_TAKEN": "Une entreprise avec cet identifiant fiscal existe déjà",
    "COORDINATES_INCOMPLETE": "Indiquez la latitude et la longitude du point sur la carte",
    "COUNTRY_INVALID": "Choisissez un pays dans la liste",
    "CURP_AVAILABLE": "CURP disponible",
    "CURP_BIRTH_DATE_MISMATCH": "Le CURP indique une naissance le {document}, mais la date de naissance est le {birth}",
    "CURP_CENTURY_MISMATCH": (
        "Le CURP ne correspond pas au siècle de la date de naissance: son 17e caractère est un chiffre pour les "
        "personnes nées avant 2000 et une lettre à partir de 2000"
    ),
    "CURP_CHECK_DIGIT": "Le CURP n'est pas valide: le chiffre de contrôle ne correspond pas",
    "CURP_DATE_INVALID": "La date du CURP (AAMMJJ) n'est pas valide",
    "CURP_FORMAT": "Le format du CURP n'est pas valide (p. ex. HEGG560427MVZRRL04)",
    "CURP_LENGTH": {
        "one": "Le CURP comporte {length} caractères; {count} a été saisi",
        "other": "Le CURP comporte {length} caractères; {count} ont été saisis",
    },
    "CURP_TAKEN": "Ce CURP est déjà enregistré pour un autre employé",
    "DEPARTMENTS_LISTED": {
        "one": "{count} service",
        "other": "{count} services",
    },
    "DEPARTMENT_CREATED": "Service créé",
    "DEPARTMENT_DELETED": "Service supprimé",
    "DEPARTMENT_EMPLOYEE_ASSIGNED": "Employé affecté",
    "DEPARTMENT_EMPLOYEE_REMOVED": "Employé retiré du service",
    "DEPARTMENT_FOUND": "Service trouvé",
    "DEPARTMENT_HAS_EMPLOYEES": (
        "Le service a des employés affectés: réaffectez-les ou retirez-les avant de le supprimer"
    ),
    "DEPARTMENT_MANAGER_ADDED": "Responsable ajouté",
    "DEPARTMENT_MANAGER_REMOVED": "Responsable retiré",
    "DEPARTMENT_NAME_TAKEN": "Un service portant ce nom existe déjà dans votre entreprise",
    "DEPARTMENT_NOT_FOUND": "Service introuvable",
    "DEPARTMENT_RESTORED": "Service restauré",
    "DEPARTMENT_UPDATED": "Service mis à jour",
    "DEVICES_LISTED": {
        "one": "{count} appareil",
        "other": "{count} appareils",
    },
    "DEVICE_STATUS_UPDATED": "Appareil mis à jour",
    "DOCUMENT_OPTIONAL": "Facultatif: peut rester vide",
    "EMAIL_AVAILABLE": "Adresse e-mail disponible",
    "EMAIL_INVALID": "Adresse e-mail non valide",
    "EMAIL_REQUIRED": "L'adresse e-mail est obligatoire",
    "EMAIL_TAKEN": "Cette adresse e-mail est déjà enregistrée sur la plateforme",
    "EMPLOYEES_GONE": "Certains employés n'existent plus: actualisez la liste",
    "EMPLOYEES_LISTED": {
        "one": "{count} employé trouvé",
        "other": "{count} employés trouvés",
    },
    "EMPLOYEES_ONLY": "Seuls les employés peuvent utiliser cette fonction",
    "EMPLOYEE_ACTIVATED": "Employé activé",
    "EMPLOYEE_CREATED": "Employé ajouté",
    "EMPLOYEE_DEACTIVATED": "Employé désactivé",
    "EMPLOYEE_DELETED": "Employé supprimé",
    "EMPLOYEE_DEVICE_APPROVED": "Appareil approuvé: il n'est plus traité comme inconnu",
    "EMPLOYEE_DEVICE_REVOKED": "Appareil révoqué: il est désormais traité comme inconnu",
    "EMPLOYEE_DUPLICATE": (
        "L'adresse e-mail, le téléphone, le numéro d'employé, le RFC, le CURP ou le NSS est déjà enregistré"
    ),
    "EMPLOYEE_FOUND": "Employé trouvé",
    "EMPLOYEE_IDS": {
        "one": "{count} employé dans le filtre",
        "other": "{count} employés dans le filtre",
    },
    "EMPLOYEE_IDS_REPEATED": "Chaque employé ne doit apparaître qu'une seule fois",
    "EMPLOYEE_INACTIVE": "L'employé est inactif",
    "EMPLOYEE_LIMIT_REACHED": (
        "Votre entreprise a atteint sa limite de {count} employés. Contactez l'administrateur de la plateforme."
    ),
    "EMPLOYEE_NOT_FOUND": "Employé introuvable",
    "EMPLOYEE_NUMBER_AVAILABLE": "Numéro d'employé disponible",
    "EMPLOYEE_NUMBER_INVALID": (
        "Le numéro d'employé doit comporter de 1 à 30 caractères: lettres, chiffres, trait d'union ou tiret bas"
    ),
    "EMPLOYEE_NUMBER_TAKEN": "Ce numéro d'employé est déjà enregistré",
    "EMPLOYEE_RESTORED": "Employé restauré. Son visage doit être enregistré à nouveau.",
    "EMPLOYEE_TOO_YOUNG": "L'employé doit avoir au moins {count} ans",
    "EMPLOYEE_UPDATED": "Employé mis à jour",
    "ENROLLMENT_RESET_BY_COMPANY": "Enregistrement réinitialisé par l'entreprise",
    "FACE_ERASED_ON_DELETE": (
        "Vos données faciales ont été effacées lors de la suppression de votre fiche. Enregistrez votre visage à "
        "nouveau."
    ),
    "FACE_NOT_APPROVED": "L'employé n'a pas encore de visage approuvé: enregistrez-le d'abord",
    "FACE_NOT_ENROLLED_YET": "Vous devez d'abord enregistrer votre visage",
    "FACE_PENDING_REVIEW": "Votre enregistrement du visage est en cours de validation par votre entreprise",
    "FACE_REJECTED_ENROLL_AGAIN": "Votre enregistrement du visage a été refusé. Enregistrez votre visage à nouveau.",
    "FIELD_NOT_ALLOWED": "Vous ne pouvez pas valider ce champ",
    "FIELD_REQUIRED": "Ce champ est obligatoire",
    "IDENTITY_REVERIFY_REQUESTED": "L'employé devra enregistrer son visage à nouveau",
    "IDENTITY_REVERIFY_REQUESTED_ALL": {
        "one": "{count} employé devra enregistrer son visage à nouveau",
        "other": "{count} employés devront enregistrer leur visage à nouveau",
    },
    "LAST_COMPANY_ADMIN": "L'entreprise doit conserver au moins un administrateur actif",
    "LEGAL_NAME_REQUIRED": "La raison sociale est obligatoire",
    "LOCATION_POINT_REQUIRED": "Pour exiger la position, marquez sur la carte le point de l'adresse",
    "LOCATION_RADIUS_REQUIRED": "Indiquez en mètres le rayon dans lequel le validateur peut se connecter",
    "MAX_CHARACTERS": "{count} caractères au maximum",
    "NAME_AVAILABLE": "Nom disponible",
    "NAME_INVALID_CHARACTERS": (
        "Seuls les lettres, les espaces, les apostrophes, les points et les traits d'union sont autorisés"
    ),
    "NAME_REQUIRED": "Le nom est obligatoire",
    "NAME_TOO_LONG": "Le nom peut comporter jusqu'à {count} caractères",
    "NSS_AVAILABLE": "NSS disponible",
    "NSS_CHECK_DIGIT": "Le NSS n'est pas valide: le chiffre de contrôle ne correspond pas",
    "NSS_LENGTH": "Le NSS comporte {count} chiffres",
    "NSS_TAKEN": "Ce NSS est déjà enregistré pour un autre employé",
    "PASSWORD_NEEDS_DIGIT": "Le mot de passe doit contenir au moins un chiffre",
    "PASSWORD_NEEDS_LOWERCASE": "Le mot de passe doit contenir au moins une lettre minuscule",
    "PASSWORD_NEEDS_UPPERCASE": "Le mot de passe doit contenir au moins une lettre majuscule",
    "PASSWORD_REQUIRED": "Le mot de passe est obligatoire pour une nouvelle personne",
    "PASSWORD_TOO_LONG": "Le mot de passe ne doit pas dépasser {count} caractères",
    "PASSWORD_TOO_SHORT": "Le mot de passe doit comporter au moins {count} caractères",
    "PHONE_AVAILABLE": "Téléphone disponible",
    "PHONE_COUNTRY_UNAVAILABLE": "Les téléphones de ce pays ne sont pas acceptés",
    "PHONE_INVALID": "Le téléphone n'est pas valide",
    "PHONE_INVALID_FOR_CODE": "Le téléphone n'est pas valide pour l'indicatif +{code}",
    "PHONE_REQUIRED": "Le téléphone est obligatoire",
    "PHONE_VALID": "Téléphone valide",
    "POSTAL_CODE_INVALID": "Le code postal n'est pas valide",
    "POSTAL_CODE_MX": "Le code postal mexicain comporte 5 chiffres",
    "QR_REVOKED": "Code QR invalidé",
    "QR_SUMMARY": "Activité du code QR",
    "RESTORE_CURP_TAKEN": "Restauration impossible: un autre employé a déjà le CURP {value}",
    "RESTORE_EMPLOYEE_NUMBER_TAKEN": "Restauration impossible: un autre employé a déjà le numéro {value}",
    "RESTORE_EMPLOYMENT_TAKEN": (
        "Restauration impossible: cette personne a déjà une autre fiche active dans votre entreprise"
    ),
    "RESTORE_NSS_TAKEN": "Restauration impossible: un autre employé a déjà le NSS {value}",
    "RESTORE_RFC_TAKEN": "Restauration impossible: un autre employé a déjà le RFC {value}",
    "REVERIFY_DEFAULT_REASON": "Votre entreprise vous demande de vérifier à nouveau votre identité",
    "RFC_AVAILABLE": "RFC disponible",
    "RFC_BIRTH_DATE_MISMATCH": "Le RFC indique une naissance le {document}, mais la date de naissance est le {birth}",
    "RFC_DATE_INVALID": "La date du RFC (AAMMJJ) n'est pas valide",
    "RFC_FORMAT": "Le format du RFC n'est pas valide (p. ex. PEGJ900515AB1)",
    "RFC_GENERIC": "Le RFC générique n'est pas valide. Saisissez le RFC de la personne.",
    "RFC_LENGTH": {
        "one": "Le RFC d'une personne physique comporte {length} caractères; {count} a été saisi",
        "other": "Le RFC d'une personne physique comporte {length} caractères; {count} ont été saisis",
    },
    "RFC_TAKEN": "Ce RFC est déjà enregistré pour un autre employé",
    "SHARED_ACCOUNT": (
        "Cette personne travaille aussi pour une autre entreprise. Son adresse e-mail, son téléphone et son mot de "
        "passe ne peuvent pas être modifiés ici."
    ),
    "TAX_ID_AVAILABLE": "Identifiant fiscal disponible",
    "TAX_ID_CHECK_DIGIT": "{name}: le chiffre de contrôle ne correspond pas. Vérifiez le numéro.",
    "TAX_ID_FORMAT": "{name}: format non valide (p. ex. {example})",
    "TAX_ID_LENGTH": "{name}: le numéro doit comporter de {min} à {max} caractères",
    "TAX_ID_LENGTH_EXACT": "{name}: le numéro doit comporter {length} caractères",
    "TAX_ID_TYPE_COUNTRY": "{name} n'est pas un identifiant fiscal utilisé dans ce pays ({country})",
    "TAX_ID_TYPE_INVALID": "Choisissez un type d'identifiant dans la liste",
    "TRADE_NAME_REQUIRED": "Le nom commercial est obligatoire",
    "VALIDATION_COMPANY_REQUIRED": "Cette validation est réservée aux comptes d'entreprise",
    "VALIDATOR_CREATED": "Validateur ajouté",
    "VALIDATOR_DELETED": "Validateur supprimé",
    "VALIDATOR_FOUND": "Validateur trouvé",
    "VALIDATOR_NAME_REQUIRED": "Le nom du validateur est obligatoire",
    "VALIDATOR_NOT_FOUND": "Validateur introuvable",
    "VALIDATOR_PASSWORD_RESET": "Mot de passe réinitialisé",
    "VALIDATOR_RESTORED": "Validateur restauré",
    "VALIDATOR_STATUS_UPDATED": "Validateur mis à jour",
    "VALIDATOR_UPDATED": "Validateur mis à jour",
    "VERIFICATIONS_LISTED": {
        "one": "{count} tentative de vérification",
        "other": "{count} tentatives de vérification",
    },
}
