"""Documentos de la empresa (ADMIN y COMPANY): subir, descargar, eliminar y restaurar los archivos que se guardan para
facturarle.

Textos en portugués de Brasil (pt-BR, trato de «você»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`); la forma `one` también sirve para el 0 (CLDR)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "COMPANY_DOCUMENTS": {
        "one": "{count} documento",
        "other": "{count} documentos",
    },
    "COMPANY_DOCUMENT_DELETED": "Documento excluído",
    "COMPANY_DOCUMENT_FILE": "Documento",
    "COMPANY_DOCUMENT_RESTORED": "Documento restaurado",
    "COMPANY_DOCUMENT_UPLOADED": "Documento salvo",
    "DOCUMENT_CONFIRMED": "A empresa já revisou este documento e você não pode excluí-lo.",
    "DOCUMENT_DAMAGED": "O arquivo está danificado ou incompleto. Escolha outro.",
    "DOCUMENT_EMPTY": "O arquivo está vazio. Escolha outro.",
    "DOCUMENT_FORMAT_NOT_ALLOWED": "O arquivo deve ser PDF, Word, Excel, XML, JPG ou PNG",
    "DOCUMENT_IMAGE_TOO_LARGE": "A imagem passa de {max} megapixels. Use uma imagem menor.",
    "DOCUMENT_MACROS_NOT_ALLOWED": "O arquivo tem macros. Salve-o sem macros (DOCX ou XLSX) e tente novamente.",
    "DOCUMENT_NOT_FOUND": "Documento não encontrado",
    "DOCUMENT_TOO_LARGE": "O arquivo excede o tamanho máximo de {size}",
    "DOCUMENT_TYPE_INVALID": "Escolha um tipo de documento da lista",
    "DOCUMENT_UPLOADED_BY_PLATFORM": (
        "Este documento foi enviado pela plataforma. Só o administrador dela pode excluí-lo ou restaurá-lo."
    ),
    "DOCUMENT_XML_UNSAFE": "O XML declara um tipo de documento ou entidades (DTD). Envie o XML sem DTD.",
    "EMPLOYEE_DOCUMENTS": {
        "one": "{count} documento",
        "other": "{count} documentos",
    },
    "EMPLOYEE_DOCUMENT_DATA_SAVED": "Dados do documento salvos",
    "EMPLOYEE_DOCUMENT_DELETED": "Documento excluído",
    "EMPLOYEE_DOCUMENT_FILE": "Documento",
    "EMPLOYEE_DOCUMENT_REQUIREMENTS": "Documentos exigidos",
    "EMPLOYEE_DOCUMENT_RESTORED": "Documento restaurado",
    "EMPLOYEE_DOCUMENT_TOO_LARGE": "O arquivo excede o tamanho máximo de {size}",
    "EMPLOYEE_DOCUMENT_UPLOADED": "Documento enviado",
}
