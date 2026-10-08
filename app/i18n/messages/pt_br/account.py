"""La cuenta: iniciar y cerrar sesión, sesiones, contraseña, dispositivos y ubicación de un validador, foto de perfil y
QR propio.

Textos en portugués de Brasil (pt-BR, trato de «você»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`); la forma `one` también sirve para el 0 (CLDR)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "AUTH_BUSY": "Há muitos acessos neste momento. Tente novamente em alguns segundos.",
    "AVATAR": "Foto de perfil",
    "AVATAR_CROP_INVALID": "O recorte precisa de `crop_x`, `crop_y` e `crop_size`",
    "AVATAR_CROP_OUTSIDE": "O recorte deve ficar dentro da imagem ({width} × {height} px) e medir pelo menos {min} px",
    "AVATAR_DAMAGED": "A imagem está danificada ou incompleta. Escolha outra.",
    "AVATAR_EMPTY": "Nenhuma imagem foi recebida",
    "AVATAR_FORMAT": "A foto deve ser uma imagem JPG, PNG ou WEBP",
    "AVATAR_NOT_FOUND": "Esta pessoa não tem foto de perfil",
    "AVATAR_REMOVED": "Foto de perfil removida",
    "AVATAR_SIZE_INVALID": "O tamanho da foto deve ser 96 ou 512",
    "AVATAR_TOO_LARGE": "A foto excede o tamanho máximo de {size}",
    "AVATAR_TOO_MANY_PIXELS": "A imagem passa de {max} megapixels",
    "AVATAR_TOO_SMALL": "A imagem é pequena demais: cada lado deve medir pelo menos {min} px",
    "AVATAR_UPDATED": "Foto de perfil salva",
    "COMPANY_INACTIVE": "Sua empresa está desativada na plataforma. Entre em contato com o administrador.",
    "COMPANY_SELECTED": "Você entrou em {company}",
    "COMPANY_SUSPENDED": (
        "Sua empresa está suspensa. Entre em contato com o administrador da plataforma para reativá-la."
    ),
    "CURRENT_PASSWORD_INVALID": "A senha atual não está correta",
    "DEVICE_DESKTOP": "Computador",
    "DEVICE_INVALID_TRANSITION": "Essa alteração não se aplica à situação atual do dispositivo",
    "DEVICE_LOCATION_INACCURATE": (
        "A localização do seu dispositivo não é precisa (±{accuracy}). Ative a localização precisa ou o GPS e tente "
        "novamente."
    ),
    "DEVICE_NOT_FOUND": "Dispositivo não encontrado",
    "DEVICE_PENDING_APPROVAL": (
        "Este dispositivo foi registrado como “{name}” e aguarda a autorização da sua empresa. Peça a um "
        "administrador que o autorize em Validadores › Dispositivos."
    ),
    "DEVICE_PHONE": "Celular",
    "DEVICE_PROOF_INVALID": "Não foi possível verificar este dispositivo. Entre novamente.",
    "DEVICE_PROOF_REQUIRED": (
        "Este validador só funciona em dispositivos autorizados pela empresa: falta verificar este dispositivo"
    ),
    "DEVICE_REJECTED": "Sua empresa não autorizou este dispositivo (“{name}”). Use um dispositivo autorizado.",
    "DEVICE_REVOKED": "Sua empresa retirou a autorização deste dispositivo (“{name}”). Peça que o autorizem novamente.",
    "DEVICE_TABLET": "Tablete",
    "INVALID_CREDENTIALS": "E-mail ou senha incorretos",
    "JWKS": "Chaves públicas de assinatura",
    "LOCATION_ACCURACY_MISSING": (
        "Seu dispositivo não informou a precisão da sua localização. Ative a localização precisa ou o GPS e tente "
        "novamente."
    ),
    "LOCATION_OUT_OF_RANGE": (
        "Você está a {distance} do local deste validador. Aproxime-se a menos de {radius} para entrar."
    ),
    "LOCATION_REQUIRED": (
        "Este validador só pode entrar no sistema a partir do seu local de operação. Permita o acesso à sua "
        "localização."
    ),
    "LOGGED_OUT": "Sessão encerrada",
    "LOGGED_OUT_ALL": {
        "one": "{count} sessão encerrada",
        "other": "{count} sessões encerradas",
    },
    "LOGIN_SUCCESS": "Sessão iniciada",
    "MY_QR": "Seu código QR",
    "MY_QR_STATUS": "Situação do seu código QR",
    "NOT_YOUR_COMPANY": "Você não trabalha nessa empresa",
    "NO_LONGER_IN_COMPANY": "Você não tem mais acesso a esta empresa. Entre novamente.",
    "PASSKEYS_LISTED": {
        "one": "{count} chave de acesso",
        "other": "{count} chaves de acesso",
    },
    "PASSKEY_ALREADY_REGISTERED": "Essa chave de acesso já está cadastrada",
    "PASSKEY_CHALLENGE_INVALID": "O desafio da chave de acesso venceu ou não é válido. Tente novamente.",
    "PASSKEY_CHALLENGE_USED": "Esse desafio já foi usado. Tente novamente.",
    "PASSKEY_CLONED": (
        "Essa chave de acesso foi usada a partir de uma cópia e foi revogada por segurança. Entre com sua senha e "
        "cadastre uma nova."
    ),
    "PASSKEY_INVALID": "Não foi possível verificar a chave de acesso enviada pelo seu dispositivo. Tente novamente.",
    "PASSKEY_LIMIT_REACHED": {
        "one": "Você já tem {count} chave de acesso. Revogue uma para cadastrar outra.",
        "other": "Você já tem {count} chaves de acesso. Revogue uma para cadastrar outra.",
    },
    "PASSKEY_LOGIN_FAILED": "Não foi possível entrar com essa chave de acesso. Tente novamente ou use sua senha.",
    "PASSKEY_LOGIN_OPTIONS": "Desafio para entrar com uma chave de acesso",
    "PASSKEY_NOT_FOUND": "Chave de acesso não encontrada",
    "PASSKEY_OPTIONS": "Desafio para cadastrar uma chave de acesso",
    "PASSKEY_REGISTERED": "Chave de acesso cadastrada",
    "PASSKEY_RENAMED": "Nome da chave salvo",
    "PASSKEY_REVOKED": "Chave de acesso revogada",
    "PASSWORD_CHANGED": {
        "zero": "Senha atualizada.",
        "one": "Senha atualizada. {count} sessão em outros dispositivos foi encerrada.",
        "other": "Senha atualizada. {count} sessões em outros dispositivos foram encerradas.",
    },
    "PASSWORD_REUSED": "A nova senha deve ser diferente da atual",
    "PREFERENCES_UPDATED": "Preferências salvas",
    "QR_DISABLED": "A verificação por QR está desativada para sua empresa. Use o reconhecimento facial.",
    "REMEMBERED_ACCOUNT": "Conta lembrada",
    "REMEMBERED_ACCOUNT_FORGOTTEN": "Este dispositivo não lembra mais a conta",
    "REMEMBERED_ACCOUNT_NONE": "Nenhuma conta lembrada",
    "SESSIONS_LISTED": {
        "one": "{count} sessão ativa",
        "other": "{count} sessões ativas",
    },
    "SESSION_ACTIVE": "Sessão ativa",
    "SESSION_NONE": "Sem sessão",
    "SESSION_NOT_FOUND": "Sessão não encontrada",
    "SESSION_NO_LONGER_VALID": "Sua sessão não é mais válida. Entre novamente.",
    "SESSION_REVOKED": "Sessão revogada",
    "TOKEN_EXPIRED": "Sua sessão expirou. Entre novamente.",
    "TOKEN_INVALID": "Sua sessão não é válida. Entre novamente.",
    "TOKEN_REFRESHED": "Sessão renovada",
    "TOUCH_DEVICE_REQUIRED": (
        "Sua empresa só permite validar identidades em um tablete ou celular. Entre por esse dispositivo com o "
        "mesmo e-mail e senha."
    ),
    "USER_INACTIVE": "A conta está desativada",
    "USER_PROFILE": "Usuário autenticado",
    "YOUR_COMPANY": "sua empresa",
}
