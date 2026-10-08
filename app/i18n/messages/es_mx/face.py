"""Identidad: registro facial, identificación, punto de control del validador y QR.

Textos en español de México (es-MX, el idioma por omisión): misma llave, mismos `{parámetros}` y mismas formas de plural
en los dos idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ANSWER_ACCEPTED": "Respuesta aceptada",
    "ANSWER_ALREADY_ACCEPTED": "Esta pregunta ya fue respondida",
    "ANSWER_INAUDIBLE": "No se escuchó tu voz. Habla más fuerte y cerca del micrófono.",
    "ANSWER_MISMATCH": "La respuesta no coincide con tus datos registrados. Responde de nuevo.",
    "ANSWER_TOO_LONG": "La respuesta es muy larga. Responde en menos de {seconds} segundos.",
    "ANSWER_TOO_SHORT": "La respuesta es muy corta. Di tu respuesta completa.",
    "ANSWER_UNCLEAR": "No se entendió tu respuesta. Habla claro y despacio.",
    "CHALLENGE_ISSUED": "Reto de prueba de vida emitido",
    "CHECKPOINT_LOCATION_OUT_OF_RANGE": (
        "Estás a {distance} del lugar de este validador. Acércate a menos de {radius} para identificar."
    ),
    "CHECKPOINT_LOCATION_REQUIRED": (
        "Este validador solo identifica en su lugar de operación. Permite el acceso a tu ubicación."
    ),
    "CHECKPOINT_PROFILE": "Validador",
    "CHECKPOINT_RECENT": "Identificaciones recientes",
    "EMPLOYEE_FACE_NOT_APPROVED": "El empleado aún no tiene un rostro aprobado: regístralo primero",
    "EMPLOYEE_INACTIVE_ENROLL": "El empleado está inactivo: actívalo antes de registrar su rostro",
    "ENROLLMENTS_LISTED": {
        "one": "{count} registro facial",
        "other": "{count} registros faciales",
    },
    "ENROLLMENT_ALREADY_APPROVED": "Tu registro facial ya fue aprobado",
    "ENROLLMENT_ALREADY_REVIEWED": "Este registro ya fue revisado",
    "ENROLLMENT_APPROVED_DONE": "Usuario aceptado. Ya puede verificar su identidad.",
    "ENROLLMENT_EMPLOYEE_INACTIVE": "Solo empleados activos pueden registrar su rostro",
    "ENROLLMENT_FOUND": "Registro facial encontrado",
    "ENROLLMENT_NOT_FOUND": "Registro facial no encontrado",
    "ENROLLMENT_PENDING": "Tu registro facial ya fue enviado y está en validación",
    "ENROLLMENT_PHOTOS_ACCEPTED": "Fotos aceptadas. Ahora responde las preguntas en video.",
    "ENROLLMENT_PHOTO_EXPIRED": "Tu foto inicial venció. Tómala de nuevo.",
    "ENROLLMENT_PHOTO_MISMATCH": "Las capturas no coinciden con tu foto inicial. Repite las capturas con tu rostro.",
    "ENROLLMENT_PHOTO_REQUIRED": "Primero toma tu foto inicial",
    "ENROLLMENT_PHOTO_SAVED": "Foto inicial guardada. Ahora sigue con las capturas.",
    "ENROLLMENT_PROGRESS": "Avance del registro facial",
    "ENROLLMENT_REJECTED": "Usuario rechazado. Deberá registrar su rostro de nuevo.",
    "ENROLLMENT_SENT": "Tu registro facial fue enviado y está en validación",
    "ENROLLMENT_SUBMITTED": "Registro facial enviado. Tu identidad está en validación.",
    "ENROLLMENT_VOICE_DONE": "Verificación por voz completada. Tu identidad está en validación.",
    "FACE_ALREADY_REGISTERED_AS": "{message} ({name}, {number})",
    "FACE_ALREADY_REGISTERED_AS_NAME": "{message} ({name})",
    "FACE_CHECK_PASSED": "La captura es válida",
    "FACE_ENROLLED_IN_PERSON": "Rostro registrado y aprobado: el empleado ya puede identificarse",
    "FACE_SIGNAL_ANTISPOOF_REAL": "Probabilidad mínima de rostro real",
    "FACE_SIGNAL_BURST_MOTION": "Movimiento natural mínimo de la ráfaga",
    "FACE_SIGNAL_FLASH_RATIO": "Cociente mínimo rostro/fondo del destello",
    "FACE_SIGNAL_FLASH_SCORE": "Respuesta mínima al destello de colores",
    "FACE_SIGNAL_LIVENESS_CLOSER": "Acercamiento mínimo a la cámara",
    "FACE_SIGNAL_LIVENESS_PITCH": "Movimiento mínimo al mirar arriba o abajo",
    "FACE_SIGNAL_LIVENESS_YAW": "Giro mínimo de la cabeza",
    "FACE_SIGNAL_MOIRE": "Patrón de pantalla máximo",
    "FACE_SIGNAL_NOISE_RATIO": "Cociente mínimo de ruido rostro/fondo",
    "FACE_SIGNAL_PARALLAX": "Paralaje mínimo al girar la cabeza",
    "FLASH_COLORS": "Colores del destello",
    "FLASH_TOKEN_INVALID": "El destello de este reto venció o no es válido. Pide otro reto.",
    "IDENTIFICATION_SUCCESS": "Identidad confirmada",
    "IMAGE_REQUIRED": "Envía al menos una captura",
    "IMAGE_VALID": "Imagen válida",
    "INVALID_FRAME_COUNT": "Envía entre {min} y {max} capturas frontales",
    "LIVENESS_NOT_REQUIRED": "Prueba de vida no requerida",
    "PHOTO_ERROR": "Foto {number}: {message}",
    "QR_FACE_MISMATCH": "El rostro no corresponde al dueño del código QR",
    "QR_HOLDER_FOUND": "Ahora valida el rostro de {name}",
    "QR_NOT_FOUND": "Código QR no encontrado",
    "QR_REQUIRED": "Escanea primero el código QR del empleado",
    "QR_WITHOUT_ATTENDANCE": "Identificado con QR. Para registrar la asistencia, identifícate con tu rostro.",
    "REJECTION_REASON_REQUIRED": "Indica el motivo del rechazo",
    "SIGNATURE_INVALID": "La firma de este dispositivo no es válida. Inicia sesión de nuevo.",
    "SIGNATURE_KEY_MISMATCH": (
        "Este no es el dispositivo con el que iniciaste sesión. Inicia sesión de nuevo en este dispositivo."
    ),
    "SIGNATURE_REQUIRED": (
        "Este validador debe firmar cada identificación con su dispositivo. "
        "Usa la aplicación en un dispositivo autorizado."
    ),
    "SIGNATURE_STALE": "La firma de este dispositivo venció. Intenta de nuevo.",
    "SPEECH_SERVICE_UNAVAILABLE": "El servicio de voz no está disponible. Intenta de nuevo en unos minutos.",
    "VALIDATOR_METHOD_NOT_ALLOWED": "Este validador identifica en modo «{mode}»",
    "VALIDATOR_REQUIRED": "Esta cuenta no es un validador de identidad",
    "VERIFICATION_LOCATION_INVALID": (
        "Tu ubicación no es válida o no es lo bastante precisa. Activa la ubicación precisa (GPS) e intenta de nuevo."
    ),
    "VERIFICATION_LOCATION_REQUIRED": (
        "Se necesita tu ubicación para verificar tu identidad. Permite el acceso a tu ubicación e intenta de nuevo."
    ),
    "VIDEO_FACE_MISMATCH": (
        "El rostro del video no coincide con tus fotos. Mantén tu rostro frente a la cámara y responde de nuevo."
    ),
    "VIDEO_TOO_LARGE": "El video es muy grande (máximo {size})",
    "VIDEO_UNSUPPORTED_FORMAT": "No se pudo leer el video. Usa Chrome, Safari, Edge o Firefox actualizados.",
    "VOICE_CLIP_FOUND": "Video de la respuesta",
    "VOICE_CLIP_NOT_FOUND": "Video no encontrado",
    "VOICE_NOT_PENDING": "Este registro no tiene una verificación por voz pendiente",
    "VOICE_RETRIES_EXHAUSTED": (
        "Se agotaron los intentos de la verificación por voz. Repite la foto inicial y las capturas."
    ),
    "VOICE_SESSION_EXPIRED": "La verificación por voz venció. Ábrela de nuevo: tus respuestas aceptadas se conservan.",
    "VOICE_SESSION_INVALID": (
        "La verificación por voz no es válida. Ábrela de nuevo: tus respuestas aceptadas se conservan."
    ),
    "VOICE_SESSION_STARTED": "Preguntas en video listas",
    "VOICE_SUM_PROMPT": "¿Cuánto es {a} más {b}?",
}
