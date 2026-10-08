"""Documentos de identidad del empleado (decisión del dueño del producto, 2026-10-07): la REFERENCIA de cada archivo,
cifrado en el bucket, y los DATOS que el OCR extrajo (texto, que la empresa confirma o corrige).

«En el onboarding se debe solicitar comprobante de domicilio e identificación oficial (pasaporte, INE); se debe extraer
la información y mostrarla en el expediente del empleado para que la empresa apruebe o rechace al empleado». Reglas
(README, «Documentos del empleado (onboarding con OCR)»; detalle en `docs/rd/documentos-ocr-2026-10-07.md`):

- **El archivo NUNCA vive en la BD**: va cifrado con `DATA_ENCRYPTION_KEY` al bucket privado por `STORED_IMAGES`
  (`EMPLOYEE_DOCUMENTS` en `app/services/image_storage.py`); aquí solo su referencia (objeto, tipo, tamaño, SHA-256 del
  objeto cifrado y cuándo se subió). Misma maquinaria que los documentos de la empresa (`document_files.inspect`).
- **Los DATOS extraídos son texto** (nombre, fecha de nacimiento, nacionalidad, vigencia, domicilio...): columnas de
  una tabla de empresa con seguridad por fila, como el RFC/CURP del empleado. Los ve y los confirma o corrige la
  EMPRESA; el ADMIN de la plataforma NO los ve (regla 13: solo la ficha de trabajo). El **número de documento** se
  cifra en reposo (`document_number_encrypted`, texto Fernet, nunca columna binaria) por ser el identificador más
  fuerte; lo demás queda en claro, protegido por la seguridad por fila y la regla de visibilidad (justificación en el
  R&D §4). El OCR es de MEJOR ESFUERZO: si no leyó algo, la columna es NULL y la empresa lo captura.
- **Tabla de empresa** (regla 14): `company_id NOT NULL`, FK compuesta al empleado `(employee_id, company_id)`,
  seguridad por fila (`TENANT_TABLES`). No crece sin límite (unos pocos documentos por empleado): no se particiona.
- **Borrado lógico** (regla 20) de lo que el PROPIO empleado elimina de su lista: reemplaza o quita los suyos mientras
  la empresa no los confirme (la empresa no borra: confirma o rechaza al empleado por el flujo del registro facial).
  Esos van a «Eliminados» por `SOFT_DELETE_RETENTION_DAYS` y la depuración borra la fila y encola su objeto para salir
  del bucket.
- **Al eliminar al empleado, sus documentos se borran DE VERDAD con la persona** (decisión del dueño del producto,
  2026-10-07; regla 13, LFPDPPP: una identificación oficial lleva la foto y los datos de la persona —no es el
  expediente de una empresa—): `person_erasure.erase_employee_documents` encola sus objetos del bucket y borra TODAS sus
  filas (vigentes y las que él mismo había eliminado) en la misma transacción del borrado lógico del empleado, como la
  biometría y las fotos. Restaurarlo NO los regresa (los vuelve a subir). Por eso un empleado eliminado ya no tiene
  documentos: la depuración `SOFT_DELETE_PURGES` de esta tabla solo cubre lo que el propio empleado eliminó de su lista.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import CATALOG, WORKFORCE
from app.core.soft_delete import SoftDeleteMixin
from app.models.mixins import StoredFileReference, company_fk, trash_index

#: Largos de los datos que escribe o corrige una persona (nombre ya limpio).
DOCUMENT_FILE_NAME_MAX = 200
EXTRACTED_NAME_MAX = 200
EXTRACTED_ADDRESS_MAX = 300
#: Catálogo de los tipos de documento del empleado (`catalog.employee_document_types`).
DOCUMENT_TYPES = "employee_document_types"


class EmployeeDocument(SoftDeleteMixin, StoredFileReference, Base):
    """Un documento de identidad de un empleado (comprobante de domicilio, pasaporte, INE...): su referencia en el
    bucket y los campos que el OCR extrajo (los confirma o corrige la empresa)."""

    __tablename__ = "employee_documents"
    __table_args__ = (
        # Documentos vigentes de un empleado, el más reciente primero (ORDER BY id DESC) y su conteo. Empieza por la
        # empresa (multiempresa, §3.1.1) y es el índice de la FK compuesta al empleado (el CASCADE busca por
        # (employee_id, company_id), que este índice cubre como igualdad en ambas); `deleted_at` en el INCLUDE deja el
        # conteo de lo vigente sin leer la tabla.
        Index(
            "ix_employee_documents_employee",
            "company_id",
            "employee_id",
            "id",
            postgresql_include=["deleted_at"],
        ),
        # Papelera de cada empleado (lo eliminado más reciente primero) y depuración de lo eliminado.
        trash_index("employee_documents", "company_id", "employee_id", "deleted_at", "id"),
        # El empleado es de la MISMA empresa (la base lo garantiza; §3.2 estructura).
        company_fk("employee_documents", "employee_id", f"{WORKFORCE}.employees"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    #: Su empleado, de la MISMA empresa (FK compuesta `fk_employee_documents_employee_company`).
    employee_id: Mapped[int] = mapped_column(nullable=False)
    #: Tipo de documento (`catalog.employee_document_types`; catálogo que nunca se borra: FK sin índice, §3.1.8).
    type: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.employee_document_types.code"), nullable=False)
    # --- Referencia del archivo CIFRADO en el bucket (nunca sus bytes; igual que los documentos de la empresa) ---
    # El formato, la clave, el nombre del objeto, el tamaño, el SHA-256 y la fecha vienen de `StoredFileReference`.
    file_name: Mapped[str] = mapped_column(String(DOCUMENT_FILE_NAME_MAX), nullable=False)
    #: Correo literal de quien lo subió (sobrevive a que esa cuenta se elimine) y si fue el propio empleado (true) o la
    #: empresa en su lugar (false).
    uploaded_by: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_by_employee: Mapped[bool] = mapped_column(nullable=False)
    # --- Datos extraídos por OCR (texto; mejor esfuerzo; los confirma o corrige la empresa) ---
    #: Nombre completo como lo leyó el documento (MRZ o texto).
    full_name: Mapped[str | None] = mapped_column(String(EXTRACTED_NAME_MAX))
    #: Número de documento CIFRADO en reposo (texto Fernet): el identificador más fuerte (pasaporte/clave). Nunca se
    #: indexa ni se busca; solo lo ve la empresa descifrado en memoria.
    document_number_encrypted: Mapped[str | None] = mapped_column(String(255))
    birth_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    #: Nacionalidad (código ISO de 3 letras de la MRZ) y sexo (M/F) tal como los trae el documento.
    nationality: Mapped[str | None] = mapped_column(String(3))
    sex: Mapped[str | None] = mapped_column(String(1))
    #: Datos de la INE / credencial para votar (México): CURP y clave de elector.
    curp: Mapped[str | None] = mapped_column(String(18))
    voter_key: Mapped[str | None] = mapped_column(String(20))
    #: Comprobante de domicilio: código postal y un fragmento de domicilio.
    postal_code: Mapped[str | None] = mapped_column(String(10))
    address: Mapped[str | None] = mapped_column(String(EXTRACTED_ADDRESS_MAX))
    #: Metadatos del OCR (no PII): el servidor procesó la imagen (`ocr_processed`), confianza media (0-1) y si una MRZ
    #: cuadró sus dígitos verificadores. La app los muestra como señal de cuánto revisar.
    ocr_processed: Mapped[bool] = mapped_column(default=False, server_default=text("false"), nullable=False)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    mrz_verified: Mapped[bool] = mapped_column(default=False, server_default=text("false"), nullable=False)
    #: La empresa confirmó los datos extraídos (tras revisarlos o corregirlos): correo literal y cuándo.
    confirmed_by: Mapped[str | None] = mapped_column(String(255))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None
