"""Documentos de la empresa (ADMIN y COMPANY): subir, descargar, eliminar y restaurar los archivos que se guardan para
facturarle.

Textos en francés de Francia (fr-FR, registro «vous»): misma llave, mismos `{parámetros}` y mismas formas de plural
que es-MX (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "COMPANY_DOCUMENTS": {
        "one": "{count} document",
        "other": "{count} documents",
    },
    "COMPANY_DOCUMENT_DELETED": "Document supprimé",
    "COMPANY_DOCUMENT_FILE": "Document",
    "COMPANY_DOCUMENT_RESTORED": "Document restauré",
    "COMPANY_DOCUMENT_UPLOADED": "Document enregistré",
    "DOCUMENT_CONFIRMED": "L’entreprise a déjà validé ce document et vous ne pouvez pas le supprimer.",
    "DOCUMENT_DAMAGED": "Le fichier est endommagé ou incomplet. Choisissez-en un autre.",
    "DOCUMENT_EMPTY": "Le fichier est vide. Choisissez-en un autre.",
    "DOCUMENT_FORMAT_NOT_ALLOWED": "Le fichier doit être au format PDF, Word, Excel, XML, JPG ou PNG",
    "DOCUMENT_IMAGE_TOO_LARGE": "L'image dépasse {max} mégapixels. Utilisez une image plus petite.",
    "DOCUMENT_MACROS_NOT_ALLOWED": (
        "Le fichier contient des macros. Enregistrez-le sans macros (DOCX ou XLSX) et réessayez."
    ),
    "DOCUMENT_NOT_FOUND": "Document introuvable",
    "DOCUMENT_TOO_LARGE": "Le fichier dépasse la taille maximale de {size}",
    "DOCUMENT_TYPE_INVALID": "Choisissez un type de document dans la liste",
    "DOCUMENT_UPLOADED_BY_PLATFORM": (
        "Ce document a été ajouté par la plateforme. Seul son administrateur peut le supprimer ou le restaurer."
    ),
    "DOCUMENT_XML_UNSAFE": "Le XML déclare un type de document ou des entités (DTD). Importez le XML sans DTD.",
    "EMPLOYEE_DOCUMENTS": {
        "one": "{count} document",
        "other": "{count} documents",
    },
    "EMPLOYEE_DOCUMENT_DATA_SAVED": "Données du document enregistrées",
    "EMPLOYEE_DOCUMENT_DELETED": "Document supprimé",
    "EMPLOYEE_DOCUMENT_FILE": "Document",
    "EMPLOYEE_DOCUMENT_REQUIREMENTS": "Documents requis",
    "EMPLOYEE_DOCUMENT_RESTORED": "Document restauré",
    "EMPLOYEE_DOCUMENT_TOO_LARGE": "Le fichier dépasse la taille maximale de {size}",
    "EMPLOYEE_DOCUMENT_UPLOADED": "Document importé",
}
