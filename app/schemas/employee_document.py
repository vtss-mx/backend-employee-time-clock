"""Documentos de identidad del empleado (decisión del dueño del producto, 2026-10-07): el contrato de la API para
subirlos en el onboarding (OCR de mejor esfuerzo) y revisarlos en el expediente (datos editables que la empresa
confirma o corrige). El archivo vive cifrado en el bucket; la API entrega su referencia, sus datos extraídos y, al
descargarlo, su contenido en base64 (como los documentos de la empresa).

Los datos extraídos (nombre, número de documento, fecha de nacimiento...) son PII que ve la EMPRESA (y el propio
empleado); el ADMIN de la plataforma NO los ve (regla 13: solo la ficha de trabajo). No tienen endpoints del ADMIN.
"""

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.employee_document import EXTRACTED_ADDRESS_MAX, EXTRACTED_NAME_MAX
from app.schemas.common import Deletion, Page

#: Largos de los datos que la empresa corrige (los mismos de las columnas del modelo).
_NUMBER_MAX = 60
_CURP_MAX = 18
_VOTER_KEY_MAX = 20
_POSTAL_MAX = 10
_NATIONALITY_MAX = 3


class EmployeeDocumentData(BaseModel):
    """Los datos que el OCR extrajo (mejor esfuerzo) o que la empresa confirmó o corrigió. Todo opcional: lo que no se
    leyó es null y la empresa lo captura. Son texto (los edita una persona)."""

    full_name: str | None = Field(default=None, max_length=EXTRACTED_NAME_MAX)
    document_number: str | None = Field(default=None, max_length=_NUMBER_MAX)
    birth_date: date | None = None
    expiry_date: date | None = None
    nationality: str | None = Field(default=None, max_length=_NATIONALITY_MAX)
    #: Sexo tal como lo trae el documento (M/F).
    sex: str | None = Field(default=None, max_length=1)
    curp: str | None = Field(default=None, max_length=_CURP_MAX)
    #: Clave de elector del INE (México).
    voter_key: str | None = Field(default=None, max_length=_VOTER_KEY_MAX)
    postal_code: str | None = Field(default=None, max_length=_POSTAL_MAX)
    address: str | None = Field(default=None, max_length=EXTRACTED_ADDRESS_MAX)


class EmployeeDocumentRead(Deletion):
    """Un documento del empleado: su referencia, los metadatos del OCR y los datos extraídos (la empresa y el propio
    empleado los ven; el ADMIN no)."""

    id: int
    employee_id: int
    #: Código de `catalog.employee_document_types` (la app muestra su nombre del catálogo).
    type: str
    file_name: str
    content_type: str
    #: Tamaño del archivo en bytes (la app lo muestra en MB).
    size: int
    uploaded_by: str
    #: Lo subió el propio empleado (true) o la empresa en su lugar (false).
    uploaded_by_employee: bool
    uploaded_at: datetime
    #: El servidor procesó la imagen con OCR, la confianza media (0-1) y si una MRZ cuadró sus dígitos verificadores.
    ocr_processed: bool
    ocr_confidence: float | None = None
    mrz_verified: bool
    #: La empresa confirmó los datos (tras revisarlos o corregirlos).
    confirmed: bool = False
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    #: Los datos extraídos/confirmados (el número de documento ya descifrado para quien puede verlo).
    data: EmployeeDocumentData


class EmployeeDocumentList(Page[EmployeeDocumentRead]):
    """Documentos de un empleado: los vigentes (el más reciente primero) o su papelera."""


class EmployeeDocumentFile(BaseModel):
    """El archivo para descargarlo: su nombre, su formato real y su contenido en base64 (descifrado en memoria)."""

    file_name: str
    content_type: str
    size: int
    data: str


class DocumentTypeOption(BaseModel):
    """Un tipo de documento que el empleado puede subir y a qué grupo del requisito pertenece."""

    code: str
    #: Grupo del requisito: `OFFICIAL_ID` (una identificación oficial) o `PROOF_OF_ADDRESS` (comprobante de domicilio).
    category: str


class EmployeeDocumentRequirements(BaseModel):
    """Lo que el onboarding del empleado necesita: si la empresa exige documentos, los tipos disponibles por grupo, lo
    que ya subió y qué le falta."""

    #: La empresa exige documentos en el onboarding (la decide el ADMIN; `companies.require_employee_documents`).
    required: bool
    types: list[DocumentTypeOption]
    documents: list[EmployeeDocumentRead]
    #: Qué falta para cumplir (solo tiene sentido cuando `required`): una identificación oficial y el comprobante.
    needs_official_id: bool
    needs_proof_of_address: bool
