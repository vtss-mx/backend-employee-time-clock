"""Documentos de la empresa (decisión del dueño del producto, 2026-10-06): los archivos que la empresa y la plataforma
guardan para facturarle. El archivo vive cifrado en el bucket; la API entrega su referencia y, al descargarlo, su
contenido en base64 (el contrato único de respuesta es JSON, como el comprobante de un pago)."""

from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import Deletion, Page


class CompanyDocumentRead(Deletion):
    """Un documento de la empresa. `can_delete`: quien lo lee puede eliminarlo y restaurarlo (la empresa, solo lo que
    ella subió; el ADMIN, cualquiera): la app no decide permisos."""

    id: int
    #: Código de `catalog.company_document_types` (la app muestra su nombre del catálogo).
    type: str
    #: Nombre original, limpio y con la extensión de su formato real.
    file_name: str
    #: Formato real, reconocido por su contenido.
    content_type: str
    #: Tamaño del archivo en bytes (la app lo muestra en MB).
    size: int
    note: str | None = None
    #: Correo literal de quien lo subió y si fue la plataforma (ADMIN) o la empresa.
    uploaded_by: str
    uploaded_by_platform: bool
    uploaded_at: datetime
    can_delete: bool


class CompanyDocumentList(Page[CompanyDocumentRead]):
    """Documentos de una empresa: los vigentes (el más reciente primero) o su papelera (lo eliminado más reciente
    primero)."""


class CompanyDocumentFile(BaseModel):
    """El archivo para descargarlo: su nombre, su formato real y su contenido en base64 (descifrado en memoria)."""

    file_name: str
    content_type: str
    size: int
    data: str
