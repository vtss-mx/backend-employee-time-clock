"""Documentos de identidad del empleado (decisión del dueño del producto, 2026-10-07; README, «Documentos del empleado
(onboarding con OCR)»).

«En el onboarding se debe solicitar comprobante de domicilio e identificación oficial (pasaporte, INE); se debe extraer
la información y mostrarla en el expediente del empleado para que la empresa apruebe o rechace al empleado».

- **El empleado sube** (`upload`, pantalla EMPLOYEE_DOCUMENTS): elige un tipo activo del catálogo
  (`employee_document_types`, 422 DOCUMENT_TYPE_INVALID) y toma una foto o elige un archivo. El archivo pasa por
  `document_files.inspect` (mismo mecanismo que los documentos de la empresa: formato por contenido, sin macros, XML
  seguro, imagen sin metadatos). Si es una imagen, el servidor la lee con OCR (`app/ocr`, Tesseract en este servidor,
  regla 13) de MEJOR ESFUERZO y guarda los campos que extrajo; nunca bloquea por una lectura imperfecta. El archivo se
  cifra y sube al bucket SIN transacción abierta y la fila se inserta en una transacción corta.
- **El empleado ve** los suyos (`list`, `file`, `requirements`) y reemplaza o elimina los que la empresa aún no
  confirmó (`delete`).
- **La empresa revisa** en el expediente (pantalla COMPANY_VALIDATIONS, junto al registro facial): `employee_list`,
  `employee_file`, y confirma o corrige los datos extraídos (`update_data`, EDITABLES). Aprobar o rechazar al empleado
  es el flujo del registro facial que ya existe (`/enrollments/{id}/approve|reject`); aquí solo vive el expediente de
  documentos. El ADMIN de la plataforma NO ve estos datos (regla 13: solo la ficha de trabajo).

Privacidad (regla 13; detalle en `docs/rd/documentos-ocr-2026-10-07.md` §4): los datos son texto en una tabla de
empresa con seguridad por fila; el NÚMERO de documento se cifra en reposo (`DATA_ENCRYPTION_KEY`) y se descifra en
memoria solo para quien puede verlo (la empresa y el propio empleado).
"""

import base64
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import encrypt_bytes, try_decrypt
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.core.object_storage import StorageError
from app.models import EmployeeDocument
from app.models.employee import Employee
from app.models.employee_document import DOCUMENT_TYPES, EXTRACTED_ADDRESS_MAX, EXTRACTED_NAME_MAX
from app.ocr import OFFICIAL_ID_TYPES, PROOF_OF_ADDRESS, OcrResult
from app.ocr import extract as run_ocr
from app.repositories.aggregates import get_scoped
from app.repositories.employee_document_repository import EmployeeDocumentRepository
from app.schemas.common import PageParams, deletion_of
from app.schemas.employee_document import (
    DocumentTypeOption,
    EmployeeDocumentData,
    EmployeeDocumentFile,
    EmployeeDocumentList,
    EmployeeDocumentRead,
    EmployeeDocumentRequirements,
)
from app.services import document_files, image_storage
from app.services.catalog_service import get_catalogs
from app.services.image_storage import EMPLOYEE_DOCUMENTS
from app.services.trash import commit_restore, ensure_deleted, ensure_live

#: Formatos de imagen que el OCR sabe leer (un PDF o un Word se guardan y la empresa captura a mano).
_OCR_IMAGE_TYPES = frozenset({"image/jpeg", "image/png"})
#: Bytes al azar de la clave del objeto (32 caracteres hexadecimales): `EmployeeDocument.uid`.
_UID_BYTES = 16


@dataclass(frozen=True)
class DocumentUpload:
    """Lo que llega en el formulario: el tipo del catálogo, el archivo y su nombre original."""

    type: str
    file_name: str | None
    data: bytes


class EmployeeDocumentService:
    """Los documentos de los empleados de UNA empresa (la empresa sale de la sesión; el empleado, del id)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.documents = EmployeeDocumentRepository(db, company_id)

    # ---------------------------------------------------------------- lectura

    def _decrypt_number(self, document: EmployeeDocument) -> str | None:
        """El número de documento descifrado en memoria; None si no hay o si la llave ya no lo lee (degradado)."""
        token = document.document_number_encrypted
        if token is None:
            return None
        plain = try_decrypt(token.encode())
        return plain.decode() if plain is not None else None

    def read(self, document: EmployeeDocument) -> EmployeeDocumentRead:
        return EmployeeDocumentRead(
            id=document.id,
            employee_id=document.employee_id,
            type=document.type,
            file_name=document.file_name,
            content_type=document.content_type,
            size=document.byte_size,
            uploaded_by=document.uploaded_by,
            uploaded_by_employee=document.uploaded_by_employee,
            uploaded_at=document.uploaded_at,
            ocr_processed=document.ocr_processed,
            ocr_confidence=document.ocr_confidence,
            mrz_verified=document.mrz_verified,
            confirmed=document.confirmed,
            confirmed_by=document.confirmed_by,
            confirmed_at=document.confirmed_at,
            data=EmployeeDocumentData(
                full_name=document.full_name,
                document_number=self._decrypt_number(document),
                birth_date=document.birth_date,
                expiry_date=document.expiry_date,
                nationality=document.nationality,
                sex=document.sex,
                curp=document.curp,
                voter_key=document.voter_key,
                postal_code=document.postal_code,
                address=document.address,
            ),
            **deletion_of(document),
        )

    def list(self, employee_id: int, page: PageParams, *, deleted: bool = False) -> EmployeeDocumentList:
        items, total = self.documents.page(employee_id, offset=page.offset, limit=page.size, deleted=deleted)
        return EmployeeDocumentList.of([self.read(item) for item in items], total, page)

    def employee_list(self, employee_id: int, page: PageParams, *, deleted: bool = False) -> EmployeeDocumentList:
        """Igual que `list`, pero la pide la EMPRESA: el empleado debe existir (404 EMPLOYEE_NOT_FOUND)."""
        self._employee(employee_id)
        return self.list(employee_id, page, deleted=deleted)

    def requirements(self, employee_id: int, *, required: bool) -> EmployeeDocumentRequirements:
        """Lo que el onboarding del empleado necesita: los tipos por grupo, lo que ya subió y qué le falta."""
        catalogs = get_catalogs()
        types = [
            DocumentTypeOption(code=code, category=_category(code))
            for code in (row["code"] for row in catalogs.entries[DOCUMENT_TYPES] if row["active"])
        ]
        items, _ = self.documents.page(employee_id, offset=0, limit=settings.PAGE_SIZE_MAX)
        present = self.documents.live_types(employee_id)
        return EmployeeDocumentRequirements(
            required=required,
            types=types,
            documents=[self.read(item) for item in items],
            needs_official_id=required and present.isdisjoint(OFFICIAL_ID_TYPES),
            needs_proof_of_address=required and PROOF_OF_ADDRESS not in present,
        )

    def file(self, employee_id: int, document_id: int) -> EmployeeDocumentFile:
        """El archivo de un documento vigente del empleado, del bucket (verificado y descifrado en memoria)."""
        document = self._get(employee_id, document_id)
        self.db.commit()  # fin de la lectura ANTES de esperar al bucket (expire_on_commit=False: los datos siguen)
        try:
            data = image_storage.read(EMPLOYEE_DOCUMENTS, document)
        except StorageError as exc:
            raise image_storage.unavailable(exc) from exc
        content = data or b""  # la referencia es NOT NULL: `read` siempre trae el archivo
        return EmployeeDocumentFile(
            file_name=document.file_name,
            content_type=document.content_type,
            size=len(content),
            data=base64.b64encode(content).decode(),
        )

    def employee_file(self, employee_id: int, document_id: int) -> EmployeeDocumentFile:
        self._employee(employee_id)
        return self.file(employee_id, document_id)

    # ---------------------------------------------------------------- escritura

    def upload(
        self, employee_id: int, upload: DocumentUpload, *, by_employee: bool, actor_email: str
    ) -> EmployeeDocumentRead:
        """Revisa el archivo, lo lee con OCR (mejor esfuerzo), lo cifra y sube, y guarda su referencia y sus datos."""
        if not get_catalogs().is_active(DOCUMENT_TYPES, upload.type):
            raise UnprocessableError(code="DOCUMENT_TYPE_INVALID", field="type")
        accepted = document_files.inspect(upload.file_name, upload.data)  # CPU, sin BD ni red
        # Fin de la lectura ANTES del OCR y del bucket: ninguna conexión espera (OCR y red sin transacción abierta).
        self.db.commit()
        ocr = self._ocr(upload.type, accepted.content_type, accepted.data)
        document = EmployeeDocument(
            company_id=self.company_id,
            employee_id=employee_id,
            type=upload.type,
            file_name=accepted.file_name,
            content_type=accepted.content_type,
            uid=secrets.token_hex(_UID_BYTES),
            uploaded_by=actor_email,
            uploaded_by_employee=by_employee,
            ocr_processed=ocr.available,
            ocr_confidence=round(ocr.confidence, 4) if ocr.available else None,
            mrz_verified=ocr.mrz_verified,
            deleted_at=None,
            deleted_by=None,
        )
        self._apply_fields(document, ocr.fields)
        try:  # red (sin transacción abierta) y después la transacción corta de la fila
            image_storage.store(self.db, EMPLOYEE_DOCUMENTS, document, accepted.data)
            self.documents.add(document)
            self.db.commit()
        except Exception:
            image_storage.abandon(self.db)  # lo subido nunca se queda sin su fila (o ya lo descartó el rollback)
            raise
        return self.read(document)

    def delete(self, employee_id: int, document_id: int, actor_email: str) -> None:
        """El empleado manda a «Eliminados» un documento suyo que la empresa AÚN no confirmó (reemplazarlo o quitarlo).
        Uno ya confirmado lo administra la empresa (409 DOCUMENT_CONFIRMED)."""
        document = self._changeable(employee_id, document_id)
        ensure_live(document)
        if document.confirmed:
            raise ConflictError(code="DOCUMENT_CONFIRMED")
        document.mark_deleted(datetime.now(UTC), actor_email)
        self.db.commit()

    def restore(self, employee_id: int, document_id: int) -> EmployeeDocumentRead:
        """El empleado regresa de «Eliminados» un documento suyo con el mismo archivo (no hay datos únicos que
        revisar)."""
        document = self._changeable(employee_id, document_id)
        ensure_deleted(document)
        document.mark_restored()
        commit_restore(self.db)
        return self.read(document)

    def update_data(
        self, employee_id: int, document_id: int, data: EmployeeDocumentData, fields_set: set[str], actor_email: str
    ) -> EmployeeDocumentRead:
        """La EMPRESA confirma o corrige los datos extraídos (solo los campos enviados cambian; los demás quedan). Al
        confirmar, queda registrado quién y cuándo."""
        self._employee(employee_id)
        document = self._changeable(employee_id, document_id)
        ensure_live(document)
        self._apply_edits(document, data, fields_set)
        document.confirmed_by = actor_email
        document.confirmed_at = datetime.now(UTC)
        self.db.commit()
        return self.read(document)

    # ---------------------------------------------------------------- internos

    def _ocr(self, document_type: str, content_type: str, data: bytes) -> OcrResult:
        """Lee los campos del documento si es una imagen; si no (PDF, Word...), un resultado vacío: la empresa captura
        a mano. Mejor esfuerzo: `run_ocr` nunca lanza."""
        if content_type not in _OCR_IMAGE_TYPES:
            return OcrResult(document_type=document_type)
        return run_ocr(document_type, data, settings.OCR_LANGUAGES)

    def _apply_fields(self, document: EmployeeDocument, fields: dict[str, str]) -> None:
        """Pasa los campos del OCR (texto) a las columnas del documento, acotados a su largo; el número va cifrado."""
        document.full_name = _clip(fields.get("full_name"), EXTRACTED_NAME_MAX)
        document.document_number_encrypted = _encrypt(fields.get("document_number"))
        document.birth_date = _as_date(fields.get("birth_date"))
        document.expiry_date = _as_date(fields.get("expiry_date"))
        document.nationality = _clip(fields.get("nationality"), 3)
        document.sex = _clip(fields.get("sex"), 1)
        document.curp = _clip(fields.get("curp"), 18)
        document.voter_key = _clip(fields.get("voter_key"), 20)
        document.postal_code = _clip(fields.get("postal_code"), 10)
        document.address = _clip(fields.get("address"), EXTRACTED_ADDRESS_MAX)

    def _apply_edits(self, document: EmployeeDocument, data: EmployeeDocumentData, fields_set: set[str]) -> None:
        """Aplica solo los campos que la empresa envió (un valor vacío o null los borra)."""
        if "document_number" in fields_set:
            document.document_number_encrypted = _encrypt(data.document_number)
        for name in (
            "full_name",
            "birth_date",
            "expiry_date",
            "nationality",
            "sex",
            "curp",
            "voter_key",
            "postal_code",
            "address",
        ):
            if name in fields_set:
                setattr(document, name, getattr(data, name))

    def _get(self, employee_id: int, document_id: int) -> EmployeeDocument:
        document = self.documents.get(document_id, employee_id)
        if document is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND")
        return document

    def _changeable(self, employee_id: int, document_id: int) -> EmployeeDocument:
        """El documento (también en «Eliminados»), bloqueado, para editarlo, eliminarlo o restaurarlo."""
        document = self.documents.get(document_id, employee_id, include_deleted=True, lock=True)
        if document is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND")
        return document

    def _employee(self, employee_id: int) -> Employee:
        """El empleado vigente de ESTA empresa (404 EMPLOYEE_NOT_FOUND: no existe, es de otra empresa o está en
        «Eliminados»). Lo usa la EMPRESA al revisar un expediente por id."""
        employee = get_scoped(self.db, Employee, employee_id, self.company_id)
        if employee is None:
            raise NotFoundError(code="EMPLOYEE_NOT_FOUND")
        return employee


def _category(code: str) -> str:
    """A qué grupo del requisito pertenece un tipo de documento (una identificación oficial o el comprobante)."""
    return "OFFICIAL_ID" if code in OFFICIAL_ID_TYPES else "PROOF_OF_ADDRESS"


def _clip(value: str | None, length: int) -> str | None:
    """El texto acotado al largo de su columna (None o vacío → None)."""
    text = (value or "").strip()
    return text[:length] or None


def _encrypt(value: str | None) -> str | None:
    """El número de documento cifrado (texto Fernet) o None; vacío = None."""
    text = (value or "").strip()
    return encrypt_bytes(text.encode()).decode() if text else None


def _as_date(value: str | None) -> date | None:
    """Una fecha ISO (`AAAA-MM-DD`) del OCR como `date`, o None si no se pudo."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
