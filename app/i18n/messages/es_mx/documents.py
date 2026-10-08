"""Documentos de la empresa (ADMIN y COMPANY): subir, descargar, eliminar y restaurar los archivos que se guardan para
facturarle.

Textos en español de México (es-MX, el idioma por omisión): misma llave, mismos `{parámetros}` y mismas formas de plural
en los dos idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "COMPANY_DOCUMENTS": {
        "one": "{count} documento",
        "other": "{count} documentos",
    },
    "COMPANY_DOCUMENT_DELETED": "Documento eliminado",
    "COMPANY_DOCUMENT_FILE": "Documento",
    "COMPANY_DOCUMENT_RESTORED": "Documento restaurado",
    "COMPANY_DOCUMENT_UPLOADED": "Documento guardado",
    "DOCUMENT_CONFIRMED": "La empresa ya revisó este documento y no puedes eliminarlo.",
    "DOCUMENT_DAMAGED": "El archivo está dañado o incompleto. Elige otro.",
    "DOCUMENT_EMPTY": "El archivo está vacío. Elige otro.",
    "DOCUMENT_FORMAT_NOT_ALLOWED": "El archivo debe ser PDF, Word, Excel, XML, JPG o PNG",
    "DOCUMENT_IMAGE_TOO_LARGE": "La imagen supera los {max} megapíxeles. Usa una imagen más pequeña.",
    "DOCUMENT_MACROS_NOT_ALLOWED": "El archivo tiene macros. Guárdalo sin macros (DOCX o XLSX) e intenta de nuevo.",
    "DOCUMENT_NOT_FOUND": "Documento no encontrado",
    "DOCUMENT_TOO_LARGE": "El archivo excede el tamaño máximo de {size}",
    "DOCUMENT_TYPE_INVALID": "Elige un tipo de documento de la lista",
    "DOCUMENT_UPLOADED_BY_PLATFORM": (
        "Este documento lo subió la plataforma. Solo su administrador puede eliminarlo o restaurarlo."
    ),
    "DOCUMENT_XML_UNSAFE": "El XML declara un tipo de documento o entidades (DTD). Sube el XML sin DTD.",
    "EMPLOYEE_DOCUMENTS": {
        "one": "{count} documento",
        "other": "{count} documentos",
    },
    "EMPLOYEE_DOCUMENT_DATA_SAVED": "Datos del documento guardados",
    "EMPLOYEE_DOCUMENT_DELETED": "Documento eliminado",
    "EMPLOYEE_DOCUMENT_FILE": "Documento",
    "EMPLOYEE_DOCUMENT_REQUIREMENTS": "Documentos del registro",
    "EMPLOYEE_DOCUMENT_RESTORED": "Documento restaurado",
    "EMPLOYEE_DOCUMENT_TOO_LARGE": "El archivo excede el tamaño máximo de {size}",
    "EMPLOYEE_DOCUMENT_UPLOADED": "Documento subido",
}
