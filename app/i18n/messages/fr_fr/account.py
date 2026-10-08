"""La cuenta: iniciar y cerrar sesión, sesiones, contraseña, dispositivos y ubicación de un validador, foto de perfil y
QR propio.

Textos en francés de Francia (fr-FR, registro «vous»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AUTH_BUSY": "Trop de connexions en ce moment. Réessayez dans quelques secondes.",
    "AVATAR": "Photo de profil",
    "AVATAR_CROP_INVALID": "Le recadrage nécessite `crop_x`, `crop_y` et `crop_size`",
    "AVATAR_CROP_OUTSIDE": "Le recadrage doit rester dans l'image ({width} × {height} px) et mesurer au moins {min} px",
    "AVATAR_DAMAGED": "L'image est endommagée ou incomplète. Choisissez-en une autre.",
    "AVATAR_EMPTY": "Aucune image n'a été reçue",
    "AVATAR_FORMAT": "La photo doit être une image JPG, PNG ou WEBP",
    "AVATAR_NOT_FOUND": "Cette personne n'a pas de photo de profil",
    "AVATAR_REMOVED": "Photo de profil supprimée",
    "AVATAR_SIZE_INVALID": "La taille de la photo doit être 96 ou 512",
    "AVATAR_TOO_LARGE": "La photo dépasse la taille maximale de {size}",
    "AVATAR_TOO_MANY_PIXELS": "L'image dépasse {max} mégapixels",
    "AVATAR_TOO_SMALL": "L'image est trop petite: chaque côté doit mesurer au moins {min} px",
    "AVATAR_UPDATED": "Photo de profil enregistrée",
    "COMPANY_INACTIVE": "Votre entreprise est désactivée sur la plateforme. Contactez l'administrateur.",
    "COMPANY_SELECTED": "Entreprise sélectionnée: {company}",
    "COMPANY_SUSPENDED": (
        "Votre entreprise est suspendue. Contactez l'administrateur de la plateforme pour la réactiver."
    ),
    "CURRENT_PASSWORD_INVALID": "Le mot de passe actuel est incorrect",
    "DEVICE_DESKTOP": "Ordinateur",
    "DEVICE_INVALID_TRANSITION": "Ce changement ne s'applique pas à l'état actuel de l'appareil",
    "DEVICE_LOCATION_INACCURATE": (
        "La position de votre appareil n'est pas précise (±{accuracy}). Activez la localisation précise ou le GPS et "
        "réessayez."
    ),
    "DEVICE_NOT_FOUND": "Appareil introuvable",
    "DEVICE_PENDING_APPROVAL": (
        "Cet appareil a été enregistré sous le nom « {name} » et attend l'autorisation de votre entreprise. Demandez à "
        "un administrateur de l'autoriser dans Validateurs › Appareils."
    ),
    "DEVICE_PHONE": "Téléphone",
    "DEVICE_PROOF_INVALID": "Impossible de vérifier cet appareil. Reconnectez-vous.",
    "DEVICE_PROOF_REQUIRED": (
        "Ce validateur ne fonctionne que sur les appareils autorisés par l'entreprise: cet appareil doit encore être "
        "vérifié"
    ),
    "DEVICE_REJECTED": "Votre entreprise n'a pas autorisé cet appareil (« {name} »). Utilisez un appareil autorisé.",
    "DEVICE_REVOKED": (
        "Votre entreprise a retiré l'autorisation de cet appareil (« {name} »). Demandez qu'il soit de nouveau "
        "autorisé."
    ),
    "DEVICE_TABLET": "Tablette",
    "INVALID_CREDENTIALS": "E-mail ou mot de passe incorrect",
    "JWKS": "Clés publiques de signature",
    "LOCATION_ACCURACY_MISSING": (
        "Votre appareil n'a pas indiqué la précision de votre position. Activez la localisation précise ou le GPS et "
        "réessayez."
    ),
    "LOCATION_OUT_OF_RANGE": (
        "Vous êtes à {distance} du lieu de ce validateur. Rapprochez-vous à moins de {radius} pour vous connecter."
    ),
    "LOCATION_REQUIRED": (
        "Ce validateur ne se connecte que sur son lieu d'utilisation. Autorisez l'accès à votre position."
    ),
    "LOGGED_OUT": "Session fermée",
    "LOGGED_OUT_ALL": {
        "one": "{count} session fermée",
        "other": "{count} sessions fermées",
    },
    "LOGIN_SUCCESS": "Session ouverte",
    "MY_QR": "Votre code QR",
    "MY_QR_STATUS": "État de votre code QR",
    "NOT_YOUR_COMPANY": "Vous ne travaillez pas dans cette entreprise",
    "NO_LONGER_IN_COMPANY": "Vous n'avez plus accès à cette entreprise. Reconnectez-vous.",
    "PASSKEYS_LISTED": {
        "one": "{count} clé d'accès",
        "other": "{count} clés d'accès",
    },
    "PASSKEY_ALREADY_REGISTERED": "Cette clé d'accès est déjà enregistrée",
    "PASSKEY_CHALLENGE_INVALID": "Le défi de la clé d'accès a expiré ou n'est pas valide. Réessayez.",
    "PASSKEY_CHALLENGE_USED": "Ce défi a déjà été utilisé. Réessayez.",
    "PASSKEY_CLONED": (
        "Cette clé d'accès a été utilisée depuis une copie et a été révoquée par sécurité. Connectez-vous avec votre "
        "mot de passe et enregistrez-en une nouvelle."
    ),
    "PASSKEY_INVALID": "Impossible de vérifier la clé d'accès envoyée par votre appareil. Réessayez.",
    "PASSKEY_LIMIT_REACHED": {
        "one": "Vous avez déjà {count} clé d'accès. Révoquez-en une pour en enregistrer une autre.",
        "other": "Vous avez déjà {count} clés d'accès. Révoquez-en une pour en enregistrer une autre.",
    },
    "PASSKEY_LOGIN_FAILED": (
        "Impossible de se connecter avec cette clé d'accès. Réessayez ou utilisez votre mot de passe."
    ),
    "PASSKEY_LOGIN_OPTIONS": "Défi pour se connecter avec une clé d'accès",
    "PASSKEY_NOT_FOUND": "Clé d'accès introuvable",
    "PASSKEY_OPTIONS": "Défi pour enregistrer une clé d'accès",
    "PASSKEY_REGISTERED": "Clé d'accès enregistrée",
    "PASSKEY_RENAMED": "Nom de la clé enregistré",
    "PASSKEY_REVOKED": "Clé d'accès révoquée",
    "PASSWORD_CHANGED": {
        "zero": "Mot de passe mis à jour.",
        "one": "Mot de passe mis à jour. {count} session a été fermée sur d'autres appareils.",
        "other": "Mot de passe mis à jour. {count} sessions ont été fermées sur d'autres appareils.",
    },
    "PASSWORD_REUSED": "Le nouveau mot de passe doit être différent de l'actuel",
    "PREFERENCES_UPDATED": "Préférences enregistrées",
    "QR_DISABLED": "La vérification par QR est désactivée pour votre entreprise. Utilisez la reconnaissance faciale.",
    "REMEMBERED_ACCOUNT": "Compte mémorisé",
    "REMEMBERED_ACCOUNT_FORGOTTEN": "Cet appareil ne mémorise plus le compte",
    "REMEMBERED_ACCOUNT_NONE": "Aucun compte mémorisé",
    "SESSIONS_LISTED": {
        "one": "{count} session active",
        "other": "{count} sessions actives",
    },
    "SESSION_ACTIVE": "Session en cours",
    "SESSION_NONE": "Aucune session",
    "SESSION_NOT_FOUND": "Session introuvable",
    "SESSION_NO_LONGER_VALID": "Votre session n'est plus valide. Reconnectez-vous.",
    "SESSION_REVOKED": "Session révoquée",
    "TOKEN_EXPIRED": "Votre session a expiré. Reconnectez-vous.",
    "TOKEN_INVALID": "Votre session n'est pas valide. Reconnectez-vous.",
    "TOKEN_REFRESHED": "Session renouvelée",
    "TOUCH_DEVICE_REQUIRED": (
        "Votre entreprise n'autorise la validation d'identité que depuis une tablette ou un téléphone. Connectez-vous "
        "depuis cet appareil avec la même adresse e-mail et le même mot de passe."
    ),
    "USER_INACTIVE": "Le compte est désactivé",
    "USER_PROFILE": "Utilisateur authentifié",
    "YOUR_COMPANY": "votre entreprise",
}
