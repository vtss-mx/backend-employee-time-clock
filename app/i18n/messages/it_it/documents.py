"""Documentos de la empresa (ADMIN y COMPANY): subir, descargar, eliminar y restaurar los archivos que se guardan para
facturarle.

Textos en italiano (it-IT), traducidos del sentido de es-MX: misma llave, mismos `{parámetros}` y mismas formas de
plural en todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "COMPANY_DOCUMENTS": {
        "one": "{count} documento",
        "other": "{count} documenti",
    },
    "COMPANY_DOCUMENT_DELETED": "Documento eliminato",
    "COMPANY_DOCUMENT_FILE": "Documento",
    "COMPANY_DOCUMENT_RESTORED": "Documento ripristinato",
    "COMPANY_DOCUMENT_UPLOADED": "Documento salvato",
    "DOCUMENT_CONFIRMED": "L'azienda ha già verificato questo documento e non puoi eliminarlo.",
    "DOCUMENT_DAMAGED": "Il file è danneggiato o incompleto. Scegline un altro.",
    "DOCUMENT_EMPTY": "Il file è vuoto. Scegline un altro.",
    "DOCUMENT_FORMAT_NOT_ALLOWED": "Il file deve essere PDF, Word, Excel, XML, JPG o PNG",
    "DOCUMENT_IMAGE_TOO_LARGE": "L'immagine supera i {max} milioni di pixel. Usa un'immagine più piccola.",
    "DOCUMENT_MACROS_NOT_ALLOWED": "Il file contiene macro. Salvalo senza macro (DOCX o XLSX) e riprova.",
    "DOCUMENT_NOT_FOUND": "Documento non trovato",
    "DOCUMENT_TOO_LARGE": "Il file supera la dimensione massima di {size}",
    "DOCUMENT_TYPE_INVALID": "Scegli un tipo di documento dall'elenco",
    "DOCUMENT_UPLOADED_BY_PLATFORM": (
        "Questo documento è stato caricato dalla piattaforma. Solo il suo amministratore può eliminarlo o "
        "ripristinarlo."
    ),
    "DOCUMENT_XML_UNSAFE": "L'XML dichiara un tipo di documento o delle entità (DTD). Carica l'XML senza DTD.",
    "EMPLOYEE_DOCUMENTS": {
        "one": "{count} documento",
        "other": "{count} documenti",
    },
    "EMPLOYEE_DOCUMENT_DATA_SAVED": "Dati del documento salvati",
    "EMPLOYEE_DOCUMENT_DELETED": "Documento eliminato",
    "EMPLOYEE_DOCUMENT_FILE": "Documento",
    "EMPLOYEE_DOCUMENT_REQUIREMENTS": "Documenti richiesti",
    "EMPLOYEE_DOCUMENT_RESTORED": "Documento ripristinato",
    "EMPLOYEE_DOCUMENT_TOO_LARGE": "Il file supera la dimensione massima di {size}",
    "EMPLOYEE_DOCUMENT_UPLOADED": "Documento caricato",
}
