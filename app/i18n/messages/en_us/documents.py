"""Documentos de la empresa (ADMIN y COMPANY): subir, descargar, eliminar y restaurar los archivos que se guardan para
facturarle.

Textos en inglés de Estados Unidos (en-US): misma llave, mismos `{parámetros}` y mismas formas de plural en los dos
idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "COMPANY_DOCUMENTS": {
        "one": "{count} document",
        "other": "{count} documents",
    },
    "COMPANY_DOCUMENT_DELETED": "Document deleted",
    "COMPANY_DOCUMENT_FILE": "Document",
    "COMPANY_DOCUMENT_RESTORED": "Document restored",
    "COMPANY_DOCUMENT_UPLOADED": "Document saved",
    "DOCUMENT_CONFIRMED": "The company already reviewed this document and you can't delete it.",
    "DOCUMENT_DAMAGED": "The file is damaged or incomplete. Choose another one.",
    "DOCUMENT_EMPTY": "The file is empty. Choose another one.",
    "DOCUMENT_FORMAT_NOT_ALLOWED": "The file must be a PDF, Word, Excel, XML, JPG, or PNG file",
    "DOCUMENT_IMAGE_TOO_LARGE": "The image exceeds {max} megapixels. Use a smaller image.",
    "DOCUMENT_MACROS_NOT_ALLOWED": "The file has macros. Save it without macros (DOCX or XLSX) and try again.",
    "DOCUMENT_NOT_FOUND": "Document not found",
    "DOCUMENT_TOO_LARGE": "The file is larger than the maximum size of {size}",
    "DOCUMENT_TYPE_INVALID": "Choose a document type from the list",
    "DOCUMENT_UPLOADED_BY_PLATFORM": (
        "The platform uploaded this document. Only its administrator can delete or restore it."
    ),
    "DOCUMENT_XML_UNSAFE": "The XML declares a document type or entities (DTD). Upload the XML without a DTD.",
    "EMPLOYEE_DOCUMENTS": {
        "one": "{count} document",
        "other": "{count} documents",
    },
    "EMPLOYEE_DOCUMENT_DATA_SAVED": "Document details saved",
    "EMPLOYEE_DOCUMENT_DELETED": "Document deleted",
    "EMPLOYEE_DOCUMENT_FILE": "Document",
    "EMPLOYEE_DOCUMENT_REQUIREMENTS": "Required documents",
    "EMPLOYEE_DOCUMENT_RESTORED": "Document restored",
    "EMPLOYEE_DOCUMENT_TOO_LARGE": "The file is larger than the maximum size of {size}",
    "EMPLOYEE_DOCUMENT_UPLOADED": "Document uploaded",
}
