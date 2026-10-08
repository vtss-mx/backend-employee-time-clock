"""Identidad: registro facial, identificación, punto de control del validador y QR.

Textos en francés de Francia (fr-FR, registro «vous»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ANSWER_ACCEPTED": "Réponse acceptée",
    "ANSWER_ALREADY_ACCEPTED": "Cette question a déjà reçu une réponse",
    "ANSWER_INAUDIBLE": "Votre voix n'a pas été entendue. Parlez plus fort et plus près du microphone.",
    "ANSWER_MISMATCH": "La réponse ne correspond pas à vos données enregistrées. Répondez à nouveau.",
    "ANSWER_TOO_LONG": "La réponse est trop longue. Répondez en moins de {seconds} secondes.",
    "ANSWER_TOO_SHORT": "La réponse est trop courte. Donnez votre réponse complète.",
    "ANSWER_UNCLEAR": "Votre réponse n'a pas été comprise. Parlez clairement et lentement.",
    "CHALLENGE_ISSUED": "Défi de détection du vivant émis",
    "CHECKPOINT_LOCATION_OUT_OF_RANGE": (
        "Vous êtes à {distance} du lieu de ce validateur. Rapprochez-vous à moins de {radius} pour identifier."
    ),
    "CHECKPOINT_LOCATION_REQUIRED": (
        "Ce validateur n'identifie que sur son lieu d'utilisation. Autorisez l'accès à votre position."
    ),
    "CHECKPOINT_PROFILE": "Validateur",
    "CHECKPOINT_RECENT": "Identifications récentes",
    "EMPLOYEE_FACE_NOT_APPROVED": "L'employé n'a pas encore de visage approuvé: enregistrez-le d'abord",
    "EMPLOYEE_INACTIVE_ENROLL": "L'employé est inactif: activez-le avant d'enregistrer son visage",
    "ENROLLMENTS_LISTED": {
        "one": "{count} enregistrement du visage",
        "other": "{count} enregistrements du visage",
    },
    "ENROLLMENT_ALREADY_APPROVED": "Votre enregistrement du visage a déjà été approuvé",
    "ENROLLMENT_ALREADY_REVIEWED": "Cet enregistrement a déjà été examiné",
    "ENROLLMENT_APPROVED_DONE": "Utilisateur accepté. Il peut désormais vérifier son identité.",
    "ENROLLMENT_EMPLOYEE_INACTIVE": "Seuls les employés actifs peuvent enregistrer leur visage",
    "ENROLLMENT_FOUND": "Enregistrement du visage trouvé",
    "ENROLLMENT_NOT_FOUND": "Enregistrement du visage introuvable",
    "ENROLLMENT_PENDING": "Votre enregistrement du visage a déjà été envoyé et est en cours de validation",
    "ENROLLMENT_PHOTOS_ACCEPTED": "Photos acceptées. Répondez maintenant aux questions en vidéo.",
    "ENROLLMENT_PHOTO_EXPIRED": "Votre photo initiale a expiré. Reprenez-la.",
    "ENROLLMENT_PHOTO_MISMATCH": (
        "Les captures ne correspondent pas à votre photo initiale. Recommencez les captures avec votre visage."
    ),
    "ENROLLMENT_PHOTO_REQUIRED": "Prenez d'abord votre photo initiale",
    "ENROLLMENT_PHOTO_SAVED": "Photo initiale enregistrée. Passez maintenant aux captures.",
    "ENROLLMENT_PROGRESS": "Avancement de l'enregistrement du visage",
    "ENROLLMENT_REJECTED": "Utilisateur refusé. Il devra enregistrer son visage à nouveau.",
    "ENROLLMENT_SENT": "Votre enregistrement du visage a été envoyé et est en cours de validation",
    "ENROLLMENT_SUBMITTED": "Enregistrement du visage envoyé. Votre identité est en cours de validation.",
    "ENROLLMENT_VOICE_DONE": "Vérification vocale terminée. Votre identité est en cours de validation.",
    "FACE_ALREADY_REGISTERED_AS": "{message} ({name}, {number})",
    "FACE_ALREADY_REGISTERED_AS_NAME": "{message} ({name})",
    "FACE_CHECK_PASSED": "La capture est valide",
    "FACE_ENROLLED_IN_PERSON": "Visage enregistré et approuvé: l'employé peut désormais s'identifier",
    "FACE_SIGNAL_ANTISPOOF_REAL": "Probabilité minimale de visage réel",
    "FACE_SIGNAL_BURST_MOTION": "Mouvement naturel minimal de la rafale",
    "FACE_SIGNAL_FLASH_RATIO": "Rapport minimal visage/fond du flash",
    "FACE_SIGNAL_FLASH_SCORE": "Réponse minimale au flash coloré",
    "FACE_SIGNAL_LIVENESS_CLOSER": "Rapprochement minimal de la caméra",
    "FACE_SIGNAL_LIVENESS_PITCH": "Mouvement minimal en regardant vers le haut ou vers le bas",
    "FACE_SIGNAL_LIVENESS_YAW": "Rotation minimale de la tête",
    "FACE_SIGNAL_MOIRE": "Motif d'écran maximal",
    "FACE_SIGNAL_NOISE_RATIO": "Rapport minimal de bruit visage/fond",
    "FACE_SIGNAL_PAD_CHROMA": "Anomalie de chroma maximale",
    "FACE_SIGNAL_PAD_COLOR": "Anomalie de couleur maximale",
    "FACE_SIGNAL_PAD_FREQUENCY": "Anomalie de fréquence maximale",
    "FACE_SIGNAL_PAD_NOISE": "Anomalie de bruit du capteur maximale",
    "FACE_SIGNAL_PAD_SHARPNESS": "Anomalie de netteté maximale",
    "FACE_SIGNAL_PAD_SPECULAR": "Reflets spéculaires maximaux",
    "FACE_SIGNAL_PAD_TEXTURE": "Anomalie de texture maximale",
    "FACE_SIGNAL_PARALLAX": "Parallaxe minimale en tournant la tête",
    "FLASH_COLORS": "Couleurs du flash",
    "FLASH_TOKEN_INVALID": "Le flash de ce défi a expiré ou n'est pas valide. Demandez un autre défi.",
    "IDENTIFICATION_SUCCESS": "Identité confirmée",
    "IMAGE_REQUIRED": "Envoyez au moins une capture",
    "IMAGE_VALID": "Image valide",
    "INVALID_FRAME_COUNT": "Envoyez entre {min} et {max} captures de face",
    "LIVENESS_NOT_REQUIRED": "Détection du vivant non requise",
    "PHOTO_ERROR": "Photo {number}: {message}",
    "QR_FACE_MISMATCH": "Le visage ne correspond pas au titulaire du code QR",
    "QR_HOLDER_FOUND": "Validez maintenant le visage de {name}",
    "QR_NOT_FOUND": "Code QR introuvable",
    "QR_REQUIRED": "Scannez d'abord le code QR de l'employé",
    "QR_WITHOUT_ATTENDANCE": (
        "Identification par QR effectuée. Pour enregistrer votre présence, identifiez-vous avec votre visage."
    ),
    "REJECTION_REASON_REQUIRED": "Indiquez le motif du refus",
    "SIGNATURE_INVALID": "La signature de cet appareil n'est pas valide. Reconnectez-vous.",
    "SIGNATURE_KEY_MISMATCH": "Cet appareil n'est pas celui de votre connexion. Reconnectez-vous sur cet appareil.",
    "SIGNATURE_REQUIRED": (
        "Ce validateur doit signer chaque identification avec son appareil. Utilisez l'application sur un appareil "
        "autorisé."
    ),
    "SIGNATURE_STALE": "La signature de cet appareil a expiré. Réessayez.",
    "SPEECH_SERVICE_UNAVAILABLE": "Le service vocal n'est pas disponible. Réessayez dans quelques minutes.",
    "VALIDATOR_METHOD_NOT_ALLOWED": "Ce validateur identifie en mode « {mode} »",
    "VALIDATOR_REQUIRED": "Ce compte n'est pas un validateur d'identité",
    "VERIFICATION_LOCATION_INVALID": (
        "Votre position n'est pas valide ou pas assez précise. Activez la position précise (GPS) et réessayez."
    ),
    "VERIFICATION_LOCATION_REQUIRED": (
        "Votre position est nécessaire pour vérifier votre identité. Autorisez l'accès à la position et réessayez."
    ),
    "VIDEO_FACE_MISMATCH": (
        "Le visage de la vidéo ne correspond pas à vos photos. Gardez votre visage face à la caméra et répondez à "
        "nouveau."
    ),
    "VIDEO_TOO_LARGE": "La vidéo est trop volumineuse (maximum {size})",
    "VIDEO_UNSUPPORTED_FORMAT": (
        "Impossible de lire la vidéo. Utilisez une version à jour de Chrome, Safari, Edge ou Firefox."
    ),
    "VOICE_CLIP_FOUND": "Vidéo de la réponse",
    "VOICE_CLIP_NOT_FOUND": "Vidéo introuvable",
    "VOICE_NOT_PENDING": "Cet enregistrement n'a pas de vérification vocale en attente",
    "VOICE_RETRIES_EXHAUSTED": (
        "Les tentatives de vérification vocale sont épuisées. Reprenez la photo initiale et les captures."
    ),
    "VOICE_SESSION_EXPIRED": "La vérification vocale a expiré. Rouvrez-la: vos réponses acceptées sont conservées.",
    "VOICE_SESSION_INVALID": (
        "La vérification vocale n'est pas valide. Rouvrez-la: vos réponses acceptées sont conservées."
    ),
    "VOICE_SESSION_STARTED": "Questions en vidéo prêtes",
    "VOICE_SUM_PROMPT": "Combien font {a} plus {b} ?",
}
