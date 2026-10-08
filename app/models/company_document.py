"""Documentos de una empresa (decisión del dueño del producto, 2026-10-06): la REFERENCIA de cada archivo, cifrado en el
bucket.

«Implementa un mecanismo para poder guardar archivos PDF, Word, etc. relacionados con la empresa para fines de emitir
facturas en el futuro» y «todos los archivos, imágenes, se deben guardar en Firebase». Por eso (README, "Documentos de
la empresa"):

- **Nunca en la BD**: el archivo va cifrado con `DATA_ENCRYPTION_KEY` al bucket privado por `STORED_IMAGES`
  (`COMPANY_DOCUMENTS` en `app/services/image_storage.py`); aquí solo su referencia (objeto, tipo, tamaño, SHA-256 del
  objeto cifrado y cuándo se subió). Se descarga solo por la API, con los permisos de cada pantalla.
- **El objeto se nombra con una clave al azar** (`uid`), no con el id de la fila: se sube ANTES de abrir la transacción
  (un archivo de hasta `COMPANY_DOCUMENT_MAX_MB` puede tardar en subir y ninguna conexión de la BD espera al bucket,
  como la foto de perfil y la evidencia de fraude), y la fila nace ya con su referencia completa (NOT NULL).
- **Tabla de empresa** (regla 14): `company_id NOT NULL`, seguridad por fila (`TENANT_TABLES`). No crece sin límite
  (unos cuantos documentos por empresa): no se particiona.
- **Borrado lógico** (regla 20): eliminar lo manda a «Eliminados» (con quién y cuándo); restaurar lo regresa con el
  mismo archivo. La depuración (`maintenance_service.SOFT_DELETE_PURGES`) borra la fila pasados
  `SOFT_DELETE_RETENTION_DAYS` y, en la misma sentencia, encola su objeto para salir del bucket; los de una empresa que
  se depura salen antes que ella.
- **Quién lo subió**: el correo literal (como la cobranza: sobrevive a que esa cuenta se elimine) y si fue la plataforma
  (el ADMIN) o la empresa: la empresa elimina y restaura solo lo que ella subió; el ADMIN, cualquiera.
"""

from sqlalchemy import Boolean, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import CATALOG, TENANCY
from app.core.soft_delete import SoftDeleteMixin
from app.models.mixins import StoredFileReference, trash_index

#: Largos de los datos que escribe una persona (nombre original ya limpio y la nota).
DOCUMENT_FILE_NAME_MAX = 200
DOCUMENT_NOTE_MAX = 300


class CompanyDocument(SoftDeleteMixin, StoredFileReference, Base):
    """Un archivo de la empresa (constancia de situación fiscal, acta constitutiva...): su referencia en el bucket."""

    __tablename__ = "company_documents"
    __table_args__ = (
        # Los documentos vigentes de la empresa, el más reciente primero (ORDER BY id DESC, leído hacia atrás) y su
        # conteo. Es también el índice de la FK hacia la empresa (§3.1.8: depurar una empresa recorre solo sus filas):
        # por eso es completo y lleva `deleted_at` en el INCLUDE, que filtra lo vigente sin leer la tabla.
        Index("ix_company_documents_company", "company_id", "id", postgresql_include=["deleted_at"]),
        # Papelera de cada empresa (lo eliminado más reciente primero) y depuración de lo eliminado.
        trash_index("company_documents", "company_id", "deleted_at", "id"),
        {"schema": TENANCY},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    #: Tipo de documento (`catalog.company_document_types`; catálogo que nunca se borra: FK sin índice, §3.1.8).
    type: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.company_document_types.code"), nullable=False)
    #: Nombre original del archivo, limpio (sin carpetas ni caracteres de control) y con la extensión de su formato
    #: real.
    file_name: Mapped[str] = mapped_column(String(DOCUMENT_FILE_NAME_MAX), nullable=False)
    #: Nota corta de quien lo subió (opcional).
    note: Mapped[str | None] = mapped_column(String(DOCUMENT_NOTE_MAX))
    # Formato, clave, nombre, tamaño, SHA-256 y fecha del objeto cifrado vienen de `StoredFileReference`.
    #: Correo literal de quien lo subió y si fue la plataforma (ADMIN) o la empresa.
    uploaded_by: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_by_platform: Mapped[bool] = mapped_column(Boolean, nullable=False)
