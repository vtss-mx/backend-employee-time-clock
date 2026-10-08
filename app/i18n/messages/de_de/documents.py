"""Documentos de la empresa (ADMIN y COMPANY): subir, descargar, eliminar y restaurar los archivos que se guardan para
facturarle.

Textos en alemán de Alemania (de-DE, trato de «Sie»): misma llave, mismos `{parámetros}` y mismas formas de plural en
todos los idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "COMPANY_DOCUMENTS": {
        "one": "{count} Dokument",
        "other": "{count} Dokumente",
    },
    "COMPANY_DOCUMENT_DELETED": "Dokument gelöscht",
    "COMPANY_DOCUMENT_FILE": "Dokument",
    "COMPANY_DOCUMENT_RESTORED": "Dokument wiederhergestellt",
    "COMPANY_DOCUMENT_UPLOADED": "Dokument gespeichert",
    "DOCUMENT_CONFIRMED": "Das Unternehmen hat dieses Dokument bereits geprüft, Sie können es nicht löschen.",
    "DOCUMENT_DAMAGED": "Die Datei ist beschädigt oder unvollständig. Wählen Sie eine andere.",
    "DOCUMENT_EMPTY": "Die Datei ist leer. Wählen Sie eine andere.",
    "DOCUMENT_FORMAT_NOT_ALLOWED": "Die Datei muss im Format PDF, Word, Excel, XML, JPG oder PNG vorliegen",
    "DOCUMENT_IMAGE_TOO_LARGE": "Das Bild hat mehr als {max} Megapixel. Verwenden Sie ein kleineres Bild.",
    "DOCUMENT_MACROS_NOT_ALLOWED": (
        "Die Datei enthält Makros. Speichern Sie sie ohne Makros (DOCX oder XLSX) und versuchen Sie es erneut."
    ),
    "DOCUMENT_NOT_FOUND": "Dokument nicht gefunden",
    "DOCUMENT_TOO_LARGE": "Die Datei überschreitet die maximale Größe von {size}",
    "DOCUMENT_TYPE_INVALID": "Wählen Sie eine Dokumentart aus der Liste",
    "DOCUMENT_UPLOADED_BY_PLATFORM": (
        "Dieses Dokument wurde von der Plattform hochgeladen. Nur ihr Administrator kann es löschen oder "
        "wiederherstellen."
    ),
    "DOCUMENT_XML_UNSAFE": (
        "Die XML-Datei deklariert einen Dokumenttyp oder Entitäten (DTD). Laden Sie die XML-Datei ohne DTD hoch."
    ),
    "EMPLOYEE_DOCUMENTS": {
        "one": "{count} Dokument",
        "other": "{count} Dokumente",
    },
    "EMPLOYEE_DOCUMENT_DATA_SAVED": "Dokumentdaten gespeichert",
    "EMPLOYEE_DOCUMENT_DELETED": "Dokument gelöscht",
    "EMPLOYEE_DOCUMENT_FILE": "Dokument",
    "EMPLOYEE_DOCUMENT_REQUIREMENTS": "Erforderliche Dokumente",
    "EMPLOYEE_DOCUMENT_RESTORED": "Dokument wiederhergestellt",
    "EMPLOYEE_DOCUMENT_TOO_LARGE": "Die Datei überschreitet die maximale Größe von {size}",
    "EMPLOYEE_DOCUMENT_UPLOADED": "Dokument hochgeladen",
}
