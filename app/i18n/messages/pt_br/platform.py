"""Plataforma (ADMIN) e integraciones: empresas, política de verificación, errores, rendimiento, consumo, llaves de la
API y casos de fraude.

Textos en portugués de Brasil (pt-BR, trato de «você»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`); la forma `one` también sirve para el 0 (CLDR). La llave de la API es la «chave da
API» y la suplantación, la «fraude de identidade», como en la aplicación web."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "ACCOUNT_DELETED_LABEL": "Conta #{id} (não existe mais)",
    "ACCOUNT_LABEL": "{email} ({role})",
    "API_DEVICE_KEY_INVALID": "A chave do dispositivo não é válida",
    "API_DEVICE_PROOF_INVALID": "Não foi possível verificar o dispositivo. Solicite um novo desafio.",
    "API_DEVICE_PROOF_REQUIRED": "Falta a prova do dispositivo: a chave, o desafio e a assinatura",
    "API_EMPLOYEE_REFERENCE_INVALID": "Envie o identificador ou o número do funcionário, apenas um dos dois",
    "API_KEYS_LISTED": {
        "one": "{count} chave",
        "other": "{count} chaves",
    },
    "API_KEY_ACCESS_DISABLED": "A empresa desta chave não tem acesso à API",
    "API_KEY_COMPANY_INACTIVE": "A empresa desta chave está desativada",
    "API_KEY_COMPANY_SUSPENDED": "A empresa desta chave está suspensa",
    "API_KEY_CREATED": "Chave criada",
    "API_KEY_EXPIRED": "Esta chave da API expirou",
    "API_KEY_INVALID": "A chave da API não é válida",
    "API_KEY_LIMIT": "Sua empresa já tem {count} chaves não revogadas. Revogue as que você não usa.",
    "API_KEY_NAME_REQUIRED": "Escreva um nome para a chave",
    "API_KEY_NOT_FOUND": "Chave não encontrada",
    "API_KEY_REQUIRED": "Falta a chave da API: envie-a no cabeçalho X-API-Key",
    "API_KEY_REVOKED": "Esta chave da API foi revogada",
    "API_KEY_REVOKED_DONE": "Chave revogada",
    "API_KEY_REVOKED_ROTATE": "Uma chave revogada não pode ser substituída: crie uma nova",
    "API_KEY_ROTATED": "Chave substituída",
    "API_KEY_VALIDATORS_DISABLED": "A empresa desta chave não tem o módulo de validadores",
    "API_SCOPE_INVALID": "Permissões inválidas: {scopes}",
    "API_SCOPE_REQUIRED": "Esta chave não tem a permissão “{scope}”. Solicite-a à sua empresa (Integrações).",
    "ATTENDANCE_FEED": {
        "one": "{count} identificação",
        "other": "{count} identificações",
    },
    "ATTENDANCE_LISTED": {
        "one": "{count} identificação",
        "other": "{count} identificações",
    },
    "CASE_STATUS_FILTER_INVALID": "Escolha uma das situações de caso disponíveis",
    "CLIENT_ERROR_RECORDED": "Falha registrada",
    "COMPANIES_LISTED": {
        "one": "{count} empresa encontrada",
        "other": "{count} empresas encontradas",
    },
    "COMPANY_ADMINS_LISTED": {
        "one": "{count} administrador",
        "other": "{count} administradores",
    },
    "COMPANY_ADMIN_CREATED": "Administrador adicionado",
    "COMPANY_ADMIN_FOUND": "Administrador encontrado",
    "COMPANY_ADMIN_PASSWORD_RESET": "Senha redefinida",
    "COMPANY_ADMIN_STATUS_UPDATED": "Administrador atualizado",
    "COMPANY_CREATED": "Empresa cadastrada",
    "COMPANY_DELETED": "Empresa excluída",
    "COMPANY_EMPLOYEES": {
        "one": "{count} funcionário",
        "other": "{count} funcionários",
    },
    "COMPANY_FOUND": "Empresa encontrada",
    "COMPANY_RESTORED": "Empresa restaurada",
    "COMPANY_UPDATED": "Empresa atualizada",
    "COMPANY_USAGE": "Consumo da empresa",
    "DRIFT_COMPANIES": {
        "one": "{count} empresa",
        "other": "{count} empresas",
    },
    "DRIFT_COMPUTED": {
        "zero": "Desvio calculado: sem tentativas na janela",
        "one": "Desvio calculado ({count} linha)",
        "other": "Desvio calculado ({count} linhas)",
    },
    "DRIFT_DISABLED": "O monitoramento de desvio está desligado na configuração do servidor",
    "DRIFT_SIGNALS": {
        "one": "{count} sinal",
        "other": "{count} sinais",
    },
    "DRIFT_SUMMARY": "Resumo do desvio dos sinais",
    "ERRORS_RESOLVED": {
        "one": "{count} erro resolvido",
        "other": "{count} erros resolvidos",
    },
    "ERROR_FILTER_REQUIRED": "Filtre por situação ou gravidade para marcar erros como resolvidos",
    "ERROR_FILTER_RESOLVED": "Esses erros já estão resolvidos",
    "ERROR_OCCURRENCES_LISTED": {
        "one": "{count} ocorrência",
        "other": "{count} ocorrências",
    },
    "ERROR_REPORTS_LISTED": {
        "one": "{count} erro",
        "other": "{count} erros",
    },
    "ERROR_REPORT_FOUND": "Erro encontrado",
    "ERROR_REPORT_NOT_FOUND": "Erro não encontrado",
    "ERROR_STATUS_UPDATED": "Acompanhamento atualizado",
    "ERROR_SUMMARY": "Resumo de erros",
    "FACE_LEARNING_FORGOTTEN": {
        "zero": "Sem amostras aprendidas: a comparação é feita só com o cadastro aprovado",
        "one": "Aprendizado reiniciado ({count} amostra): a comparação é feita só com o cadastro aprovado",
        "other": "Aprendizado reiniciado ({count} amostras): a comparação é feita só com o cadastro aprovado",
    },
    "FACE_LEARNING_SUMMARY": "Evolução do reconhecimento facial",
    "FRAUD_CASE": "Caso de fraude",
    "FRAUD_CASES": {
        "one": "{count} caso",
        "other": "{count} casos",
    },
    "FRAUD_CASES_COUNT": "Casos de fraude ativos",
    "FRAUD_CASE_DECIDED": "Caso atualizado",
    "FRAUD_CASE_NOTE": "Observação adicionada",
    "FRAUD_CASE_NOT_FOUND": "Caso de fraude não encontrado",
    "FRAUD_CASE_SAME_STATUS": "O caso já está nessa situação",
    "FRAUD_EVIDENCE": "Evidência",
    "FRAUD_EVIDENCE_NOT_FOUND": "A evidência não está mais disponível",
    "FRAUD_NOTE_REQUIRED": "Explique por que você confirma ou descarta a fraude",
    "FRAUD_NOTE_TEXT_REQUIRED": "Escreva a observação",
    "INTEGRATION_COMPANY": "Empresa da chave",
    "INVALID_ANTISPOOF_LEVEL": "Escolha um dos níveis de detecção de fraude de identidade disponíveis",
    "INVALID_CASE_STATUS": "Escolha a nova situação do caso",
    "INVALID_CONFIDENCE_LEVEL": "Escolha um dos níveis de confiança disponíveis",
    "INVALID_CURSOR": "O cursor não é válido",
    "INVALID_DEVICE_MODE": "Escolha um dos modos de dispositivo disponíveis",
    "INVALID_FLASH_MODE": "Escolha um dos modos de flash disponíveis",
    "INVALID_POLICY_PRESET": "Escolha um dos níveis predefinidos",
    "INVALID_RANGE": "A data inicial não pode ser posterior à final",
    "INVALID_RISK_ACTION": "Escolha uma das ações disponíveis",
    "INVALID_RISK_FALLBACK": "Se o mecanismo de risco falhar, escolha permitir, avisar ou pedir uma etapa a mais",
    "INVALID_RISK_SIGNAL": "Esse sinal de risco não existe",
    "INVALID_SIGNAL_MODE": "Escolha um dos modos do sinal",
    "INVALID_SINCE_UNTIL": "`since` deve ser anterior a `until`",
    "INVALID_VOICE_PROFILE": "Escolha uma das vozes disponíveis",
    "LIVENESS_MOVES_MIN": "A prova de vida precisa de pelo menos dois movimentos de cabeça ativos",
    "PERFORMANCE_METRICS": {
        "one": "{count} item",
        "other": "{count} itens",
    },
    "PERFORMANCE_OVERVIEW": "Desempenho da plataforma",
    "PERFORMANCE_SERIES": "Série do desempenho",
    "PERFORMANCE_STATEMENTS": {
        "one": "{count} consulta",
        "other": "{count} consultas",
    },
    "PERFORMANCE_WEB_VITALS": {
        "one": "{count} tela",
        "other": "{count} telas",
    },
    "PLATFORM_STATS": "Indicadores da plataforma",
    "POLICY": "Política de verificação",
    "POLICY_CHANGED_SINCE": "A política mudou depois da solicitação: é preciso solicitar a alteração novamente",
    "POLICY_CHANGES": {
        "one": "{count} alteração",
        "other": "{count} alterações",
    },
    "POLICY_CHANGE_APPROVED": "Alteração aprovada: a política já a aplica",
    "POLICY_CHANGE_CANCELLED": "Alteração cancelada",
    "POLICY_CHANGE_EXPIRED": "Expirou sem aprovação",
    "POLICY_CHANGE_NOT_FOUND": "Alteração de política não encontrada",
    "POLICY_CHANGE_NOT_PENDING": "Esta alteração já foi decidida",
    "POLICY_CHANGE_PENDING": "A alteração reduz a segurança: será aplicada quando outro administrador a aprovar",
    "POLICY_CHANGE_REJECTED": "Alteração rejeitada",
    "POLICY_NOT_REQUESTER": "Só quem solicitou a alteração pode cancelá-la",
    "POLICY_SELF_APPROVAL": "Você não pode aprovar a sua própria alteração: outro administrador deve fazer isso",
    "POLICY_SELF_DECISION": "Você não pode rejeitar a sua própria alteração: use “Retirar”",
    "POLICY_SIMULATED": {
        "one": "{count} tentativa simulada",
        "other": "{count} tentativas simuladas",
    },
    "POLICY_UNCHANGED": "Sem alterações na política",
    "POLICY_UPDATED": "Política de verificação atualizada",
    "RANGE_TOO_LONG": "Escolha um período de até um ano",
    "RESTORE_COMPANY_TAX_ID_TAKEN": (
        "Não é possível restaurar: outra empresa já tem esse identificador fiscal ({name} {value})"
    ),
    "RISK_SCORES_ORDER": "Os cortes de risco devem estar em ordem: médio < alto < crítico",
    "SERVER_STATUS": "Situação do servidor",
    "SIGNAL_MEASURE_ONLY": "Este sinal só é medido: não pode ser exigido até ser calibrado",
    "SLOW_ALERTS_LISTED": {
        "one": "{count} alerta",
        "other": "{count} alertas",
    },
    "SLOW_ALERTS_SUMMARY": "Resumo de alertas",
    "SLOW_ALERT_FOUND": "Alerta encontrado",
    "SLOW_ALERT_NOT_FOUND": "Alerta não encontrado",
    "SLOW_ALERT_STATUS_UPDATED": "Acompanhamento atualizado",
    "STORAGE_CLIENT_FAILED": "não foi possível conectar ao armazenamento do Google",
    "STORAGE_COMPANY_DOCUMENTS": "Documentos das empresas",
    "STORAGE_EMPLOYEE_DOCUMENTS": "Documentos de identidade dos funcionários",
    "STORAGE_ENROLLMENT_VOICE_CLIPS": "Vídeos da verificação por voz do cadastro facial",
    "STORAGE_FACE_ENROLLMENT_DRAFT_PHOTOS": "Fotos iniciais do cadastro facial (rascunhos)",
    "STORAGE_FACE_ENROLLMENT_PHOTOS": "Fotos de referência do cadastro facial",
    "STORAGE_FRAUD_EVIDENCE": "Quadros de evidência de casos de fraude",
    "STORAGE_KEY_INVALID": "a chave da conta de serviço não é válida",
    "STORAGE_KEY_NOT_MOUNTED": "a chave da conta de serviço ainda não está montada (arquivo vazio)",
    "STORAGE_KEY_UNREADABLE": "não foi possível ler a chave da conta de serviço ({error})",
    "STORAGE_NOT_CONFIGURED": "faltam GCS_BUCKET ou GCS_CREDENTIALS_FILE na configuração",
    "STORAGE_PAYMENT_RECEIPTS": "Comprovantes de pagamento",
    "STORAGE_PENDING_DELETIONS": "Objetos a apagar do armazenamento",
    "STORAGE_USER_AVATARS": "Fotos de perfil (um objeto por tamanho)",
    "THRESHOLDS_RECALIBRATED": {
        "zero": "Limiares recalculados: sem alterações",
        "one": "Limiares recalculados ({count} mudou)",
        "other": "Limiares recalculados ({count} mudaram)",
    },
    "TOO_MANY_SAMPLES": "Um lote aceita até {count} amostras",
    "USAGE_COMPANIES": {
        "one": "{count} empresa",
        "other": "{count} empresas",
    },
    "USAGE_OVERVIEW": "Consumo da plataforma",
    "USAGE_ROUTES": {
        "one": "{count} rota",
        "other": "{count} rotas",
    },
    "USAGE_USERS": {
        "one": "{count} conta",
        "other": "{count} contas",
    },
    "VALIDATORS_DISABLED": "Sua empresa não tem o módulo de validadores. Solicite-o ao administrador da plataforma.",
    "VALIDATORS_LISTED": {
        "one": "{count} validador",
        "other": "{count} validadores",
    },
    "VALIDATOR_LIMIT_BELOW_ACTIVE": {
        "one": "A empresa tem {count} validador ativo: o limite não pode ser menor. Peça que ela o desative primeiro.",
        "other": (
            "A empresa tem {count} validadores ativos: o limite não pode ser menor. Peça que ela desative os que "
            "sobram."
        ),
    },
    "VALIDATOR_LIMIT_REACHED": {
        "zero": "Sua empresa não tem mais vagas para validadores. Peça mais ao administrador da plataforma.",
        "one": (
            "Sua empresa atingiu o limite de {count} validador ativo. Desative um ou peça mais ao administrador da "
            "plataforma."
        ),
        "other": (
            "Sua empresa atingiu o limite de {count} validadores ativos. Desative um ou peça mais ao administrador da "
            "plataforma."
        ),
    },
    "WEB_PERFORMANCE_RECORDED": "Desempenho registrado",
}
