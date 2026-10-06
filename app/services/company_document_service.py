"""Documentos de la empresa (decisión del dueño del producto, 2026-10-06; README, "Documentos de la empresa").

«Implementa un mecanismo para poder guardar archivos PDF, Word, etc. relacionados con la empresa para fines de emitir
facturas en el futuro». Dos pantallas usan este mismo servicio, cada una con su alcance:

- **El ADMIN** (pantalla "Empresas", sección de la ficha de cada empresa): los de cualquier empresa vigente (404
  `COMPANY_NOT_FOUND` si no existe o está en «Eliminados»), para facturarle.
- **La empresa** (pantalla "Documentos"): los suyos (la empresa sale de la sesión, nunca del cliente), para no tener que
  enviarlos por correo.

Reglas:

- **Subir** (`upload`): el tipo debe ser uno activo del catálogo (`company_document_types`, 422 `DOCUMENT_TYPE_INVALID`)
  y el archivo pasa por `document_files.inspect` (formato real, sin macros, XML seguro, imagen sin metadatos, nombre
  limpio). Después se cifra y se sube al bucket SIN transacción abierta (`image_storage.store`; ninguna conexión espera
  al bucket) y la fila se inserta en una transacción corta. Sin bucket: 503 `STORAGE_UNAVAILABLE` y nada cambia; si la
  inserción falla, el objeto se borra (`image_storage.abandon` o los eventos de la sesión).
- **Descargar** (`file`): solo un documento vigente; la lectura de la base termina ANTES de esperar al bucket y el
  archivo se verifica (SHA-256), se descifra en memoria y viaja en base64 dentro del contrato. Bucket caído: 503.
- **Eliminar y restaurar** (borrado lógico, regla 20): «Eliminados» por `SOFT_DELETE_RETENTION_DAYS`; la depuración
  borra la fila y encola su objeto para salir del bucket. **La empresa elimina y restaura solo lo que ella subió**
  (decisión: lo que subió la plataforma lo administra la plataforma; 403 `DOCUMENT_UPLOADED_BY_PLATFORM`); el ADMIN,
  cualquiera. Cada documento dice a quien lo lee si puede (`can_delete`): la app no decide permisos.
"""

import base64
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, PermissionDeniedError, UnprocessableError
from app.core.object_storage import StorageError
from app.models import CompanyDocument, User, UserRole
from app.repositories.company_document_repository import CompanyDocumentRepository
from app.schemas.common import PageParams, deletion_of
from app.schemas.company_document import CompanyDocumentFile, CompanyDocumentList, CompanyDocumentRead
from app.services import document_files, image_storage
from app.services.catalog_service import get_catalogs
from app.services.company_service import CompanyService
from app.services.image_storage import COMPANY_DOCUMENTS
from app.services.trash import commit_restore, ensure_deleted, ensure_live

#: Catálogo de los tipos de documento.
DOCUMENT_TYPES = "company_document_types"
#: Bytes al azar de la clave del objeto (32 caracteres hexadecimales: `CompanyDocument.uid`).
_UID_BYTES = 16


@dataclass(frozen=True)
class DocumentUpload:
    """Lo que llega en el formulario: el tipo del catálogo, la nota (ya sin espacios de más) y el archivo."""

    type: str
    note: str | None
    file_name: str | None
    data: bytes


class CompanyDocumentService:
    """Los documentos de UNA empresa vistos por `viewer`: el ADMIN (plataforma) o un administrador de la empresa."""

    def __init__(self, db: Session, company_id: int, viewer: User) -> None:
        self.db = db
        self.company_id = company_id
        self.viewer = viewer
        self.platform = viewer.role == UserRole.ADMIN
        self.documents = CompanyDocumentRepository(db, company_id)

    def _company(self) -> None:
        """El ADMIN solo opera sobre una empresa vigente (404 `COMPANY_NOT_FOUND`); la de la empresa sale de su sesión
        (la autenticación ya la validó)."""
        if self.platform:
            CompanyService(self.db).get(self.company_id)

    def read(self, document: CompanyDocument) -> CompanyDocumentRead:
        return CompanyDocumentRead(
            id=document.id,
            type=document.type,
            file_name=document.file_name,
            content_type=document.content_type,
            size=document.byte_size,
            note=document.note,
            uploaded_by=document.uploaded_by,
            uploaded_by_platform=document.uploaded_by_platform,
            uploaded_at=document.uploaded_at,
            can_delete=self._can_change(document),
            **deletion_of(document),
        )

    def _can_change(self, document: CompanyDocument) -> bool:
        return self.platform or not document.uploaded_by_platform

    def list(self, page: PageParams, *, deleted: bool = False) -> CompanyDocumentList:
        self._company()
        items, total = self.documents.page(offset=page.offset, limit=page.size, deleted=deleted)
        return CompanyDocumentList.of([self.read(item) for item in items], total, page)

    def upload(self, upload: DocumentUpload) -> CompanyDocumentRead:
        """Revisa, cifra y sube el archivo; después guarda su referencia (ver el docstring del módulo)."""
        self._company()
        if not get_catalogs().is_active(DOCUMENT_TYPES, upload.type):
            raise UnprocessableError(code="DOCUMENT_TYPE_INVALID", field="type")
        accepted = document_files.inspect(upload.file_name, upload.data)  # CPU, sin BD ni red
        # Fin de la lectura (la empresa del ADMIN) ANTES de esperar al bucket: la conexión vuelve al pool.
        self.db.commit()
        document = CompanyDocument(
            company_id=self.company_id,
            type=upload.type,
            file_name=accepted.file_name,
            note=upload.note,
            content_type=accepted.content_type,
            uid=secrets.token_hex(_UID_BYTES),
            uploaded_by=self.viewer.email,
            uploaded_by_platform=self.platform,
            deleted_at=None,
            deleted_by=None,
        )
        try:  # red (sin transacción abierta) y después la transacción corta de la fila
            image_storage.store(self.db, COMPANY_DOCUMENTS, document, accepted.data)
            self.documents.add(document)
            self.db.commit()
        except Exception:
            image_storage.abandon(self.db)  # lo subido nunca se queda sin su fila (o ya lo descartó el rollback)
            raise
        return self.read(document)

    def file(self, document_id: int) -> CompanyDocumentFile:
        """El archivo de un documento vigente, del bucket (verificado y descifrado en memoria)."""
        self._company()
        document = self._get(document_id)
        self.db.commit()  # fin de la lectura ANTES de esperar al bucket (expire_on_commit=False: los datos siguen)
        try:
            data = image_storage.read(COMPANY_DOCUMENTS, document)
        except StorageError as exc:
            raise image_storage.unavailable(exc) from exc
        content = data or b""  # la referencia es NOT NULL: `read` siempre trae el archivo
        return CompanyDocumentFile(
            file_name=document.file_name,
            content_type=document.content_type,
            size=len(content),
            data=base64.b64encode(content).decode(),
        )

    def delete(self, document_id: int) -> None:
        """A «Eliminados» (su archivo se queda en el bucket hasta que la depuración borre la fila)."""
        self._company()
        document = self._changeable(document_id)
        ensure_live(document)
        document.mark_deleted(datetime.now(UTC), self.viewer.email)
        self.db.commit()

    def restore(self, document_id: int) -> CompanyDocumentRead:
        """Regresa de «Eliminados» con el mismo archivo (no hay datos únicos que revisar)."""
        self._company()
        document = self._changeable(document_id)
        ensure_deleted(document)
        document.mark_restored()
        commit_restore(self.db)
        return self.read(document)

    def _get(self, document_id: int) -> CompanyDocument:
        document = self.documents.get(document_id)
        if document is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND")
        return document

    def _changeable(self, document_id: int) -> CompanyDocument:
        """El documento (también en «Eliminados»), bloqueado, si quien lo pide puede eliminarlo y restaurarlo."""
        document = self.documents.get(document_id, include_deleted=True, lock=True)
        if document is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND")
        if not self._can_change(document):
            raise PermissionDeniedError(code="DOCUMENT_UPLOADED_BY_PLATFORM")
        return document
