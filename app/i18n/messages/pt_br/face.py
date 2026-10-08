"""Identidad: registro facial, identificación, punto de control del validador y QR.

Textos en portugués de Brasil (pt-BR, trato de «você»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`); la forma `one` también sirve para el 0 (CLDR). El registro facial es el «cadastro
facial» y la prueba de vida, la «prova de vida», como en la aplicación web."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ANSWER_ACCEPTED": "Resposta aceita",
    "ANSWER_ALREADY_ACCEPTED": "Esta pergunta já foi respondida",
    "ANSWER_INAUDIBLE": "Sua voz não foi ouvida. Fale mais alto e perto do microfone.",
    "ANSWER_MISMATCH": "A resposta não coincide com seus dados cadastrados. Responda novamente.",
    "ANSWER_TOO_LONG": "A resposta é muito longa. Responda em menos de {seconds} segundos.",
    "ANSWER_TOO_SHORT": "A resposta é muito curta. Diga sua resposta completa.",
    "ANSWER_UNCLEAR": "Sua resposta não foi entendida. Fale com clareza e devagar.",
    "CHALLENGE_ISSUED": "Desafio de prova de vida emitido",
    "CHECKPOINT_LOCATION_OUT_OF_RANGE": (
        "Você está a {distance} do local deste validador. Aproxime-se a menos de {radius} para identificar pessoas."
    ),
    "CHECKPOINT_LOCATION_REQUIRED": (
        "Este validador só identifica pessoas no seu local de operação. Permita o acesso à sua localização."
    ),
    "CHECKPOINT_PROFILE": "Validador",
    "CHECKPOINT_RECENT": "Identificações recentes",
    "EMPLOYEE_FACE_NOT_APPROVED": "O funcionário ainda não tem um rosto aprovado: faça o cadastro facial primeiro",
    "EMPLOYEE_INACTIVE_ENROLL": "O funcionário está inativo: ative-o antes de cadastrar o rosto dele",
    "ENROLLMENTS_LISTED": {
        "one": "{count} cadastro facial",
        "other": "{count} cadastros faciais",
    },
    "ENROLLMENT_ALREADY_APPROVED": "Seu cadastro facial já foi aprovado",
    "ENROLLMENT_ALREADY_REVIEWED": "Este cadastro já foi revisado",
    "ENROLLMENT_APPROVED_DONE": "Usuário aceito. Já pode verificar a própria identidade.",
    "ENROLLMENT_EMPLOYEE_INACTIVE": "Só funcionários ativos podem cadastrar o rosto",
    "ENROLLMENT_FOUND": "Cadastro facial encontrado",
    "ENROLLMENT_NOT_FOUND": "Cadastro facial não encontrado",
    "ENROLLMENT_PENDING": "Seu cadastro facial já foi enviado e está em validação",
    "ENROLLMENT_PHOTOS_ACCEPTED": "Fotos aceitas. Agora responda às perguntas em vídeo.",
    "ENROLLMENT_PHOTO_EXPIRED": "Sua foto inicial venceu. Tire-a de novo.",
    "ENROLLMENT_PHOTO_MISMATCH": "As capturas não coincidem com sua foto inicial. Repita as capturas com seu rosto.",
    "ENROLLMENT_PHOTO_REQUIRED": "Primeiro tire sua foto inicial",
    "ENROLLMENT_PHOTO_SAVED": "Foto inicial salva. Agora continue com as capturas.",
    "ENROLLMENT_PROGRESS": "Andamento do cadastro facial",
    "ENROLLMENT_REJECTED": "Usuário rejeitado. Será preciso cadastrar o rosto novamente.",
    "ENROLLMENT_SENT": "Seu cadastro facial foi enviado e está em validação",
    "ENROLLMENT_SUBMITTED": "Cadastro facial enviado. Sua identidade está em validação.",
    "ENROLLMENT_VOICE_DONE": "Verificação por voz concluída. Sua identidade está em validação.",
    "FACE_ALREADY_REGISTERED_AS": "{message} ({name}, {number})",
    "FACE_ALREADY_REGISTERED_AS_NAME": "{message} ({name})",
    "FACE_CHECK_PASSED": "A captura é válida",
    "FACE_ENROLLED_IN_PERSON": "Rosto cadastrado e aprovado: o funcionário já pode se identificar",
    "FACE_SIGNAL_ANTISPOOF_REAL": "Probabilidade mínima de rosto real",
    "FACE_SIGNAL_BURST_MOTION": "Movimento natural mínimo da sequência",
    "FACE_SIGNAL_FLASH_RATIO": "Razão mínima rosto/fundo do flash",
    "FACE_SIGNAL_FLASH_SCORE": "Resposta mínima ao flash de cores",
    "FACE_SIGNAL_LIVENESS_CLOSER": "Aproximação mínima da câmera",
    "FACE_SIGNAL_LIVENESS_PITCH": "Movimento mínimo ao olhar para cima ou para baixo",
    "FACE_SIGNAL_LIVENESS_YAW": "Giro mínimo da cabeça",
    "FACE_SIGNAL_MOIRE": "Padrão de tela máximo",
    "FACE_SIGNAL_NOISE_RATIO": "Razão mínima de ruído rosto/fundo",
    "FACE_SIGNAL_PAD_CHROMA": "Anomalia de croma máxima",
    "FACE_SIGNAL_PAD_COLOR": "Anomalia de cor máxima",
    "FACE_SIGNAL_PAD_FREQUENCY": "Anomalia de frequência máxima",
    "FACE_SIGNAL_PAD_NOISE": "Anomalia de ruído do sensor máxima",
    "FACE_SIGNAL_PAD_SHARPNESS": "Anomalia de nitidez máxima",
    "FACE_SIGNAL_PAD_SPECULAR": "Reflexos especulares máximos",
    "FACE_SIGNAL_PAD_TEXTURE": "Anomalia de textura máxima",
    "FACE_SIGNAL_PARALLAX": "Paralaxe mínima ao girar a cabeça",
    "FLASH_COLORS": "Cores do flash",
    "FLASH_TOKEN_INVALID": "O flash deste desafio expirou ou não é válido. Peça outro desafio.",
    "IDENTIFICATION_SUCCESS": "Identidade confirmada",
    "IMAGE_REQUIRED": "Envie pelo menos uma captura",
    "IMAGE_VALID": "Imagem válida",
    "INVALID_FRAME_COUNT": "Envie entre {min} e {max} capturas frontais",
    "LIVENESS_NOT_REQUIRED": "Prova de vida não exigida",
    "PHOTO_ERROR": "Foto {number}: {message}",
    "QR_FACE_MISMATCH": "O rosto não corresponde ao dono do código QR",
    "QR_HOLDER_FOUND": "Agora valide o rosto de {name}",
    "QR_NOT_FOUND": "Código QR não encontrado",
    "QR_REQUIRED": "Leia primeiro o código QR do funcionário",
    "QR_WITHOUT_ATTENDANCE": "Identificado com QR. Para registrar a presença, identifique-se com o rosto.",
    "REJECTION_REASON_REQUIRED": "Informe o motivo da rejeição",
    "SIGNATURE_INVALID": "A assinatura deste dispositivo não é válida. Entre novamente.",
    "SIGNATURE_KEY_MISMATCH": "Este não é o dispositivo com que você entrou. Entre novamente neste dispositivo.",
    "SIGNATURE_REQUIRED": (
        "Este validador deve assinar cada identificação com o seu dispositivo. "
        "Use o aplicativo em um dispositivo autorizado."
    ),
    "SIGNATURE_STALE": "A assinatura deste dispositivo expirou. Tente novamente.",
    "SPEECH_SERVICE_UNAVAILABLE": "O serviço de voz não está disponível. Tente novamente em alguns minutos.",
    "VALIDATOR_METHOD_NOT_ALLOWED": "Este validador identifica no modo “{mode}”",
    "VALIDATOR_REQUIRED": "Esta conta não é um validador de identidade",
    "VERIFICATION_LOCATION_INVALID": (
        "Sua localização não é válida ou não é precisa o suficiente. Ative a localização precisa (GPS) e tente de novo."
    ),
    "VERIFICATION_LOCATION_REQUIRED": (
        "É necessária sua localização para verificar sua identidade. Permita o acesso à localização e tente de novo."
    ),
    "VIDEO_FACE_MISMATCH": (
        "O rosto do vídeo não coincide com suas fotos. Mantenha o rosto diante da câmera e responda novamente."
    ),
    "VIDEO_TOO_LARGE": "O vídeo é muito grande (máximo {size})",
    "VIDEO_UNSUPPORTED_FORMAT": (
        "Não foi possível ler o vídeo. Use o Chrome, o Safari, o Edge ou o Firefox atualizados."
    ),
    "VOICE_CLIP_FOUND": "Vídeo da resposta",
    "VOICE_CLIP_NOT_FOUND": "Vídeo não encontrado",
    "VOICE_NOT_PENDING": "Este cadastro não tem uma verificação por voz pendente",
    "VOICE_RETRIES_EXHAUSTED": (
        "As tentativas da verificação por voz se esgotaram. Repita a foto inicial e as capturas."
    ),
    "VOICE_SESSION_EXPIRED": "A verificação por voz venceu. Abra-a de novo: suas respostas aceitas são mantidas.",
    "VOICE_SESSION_INVALID": (
        "A verificação por voz não é válida. Abra-a de novo: suas respostas aceitas são mantidas."
    ),
    "VOICE_SESSION_STARTED": "Perguntas em vídeo prontas",
    "VOICE_SUM_PROMPT": "Quanto é {a} mais {b}?",
}
