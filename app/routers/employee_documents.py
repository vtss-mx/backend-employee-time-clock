"""Documentos de identidad del empleado (decisión del dueño del producto, 2026-10-07): el onboarding con OCR.

Dos routers, cada uno con su pantalla y su rol (regla 2):

- `router` (EMPLOYEE, pantalla `EMPLOYEE_DOCUMENTS`): `/me/documents`, los del propio empleado. Sube (foto o archivo;
  el servidor extrae la información con OCR de mejor esfuerzo), ve lo suyo, descarga, elimina y restaura mientras la
  empresa no los confirme.
- `company_router` (COMPANY, pantalla `COMPANY_VALIDATIONS`): `/validations/employees/{employee_id}/documents`, el
  expediente del empleado junto al registro facial. La empresa ve los documentos y sus datos, los descarga y confirma
  o corrige los datos extraídos (EDITABLES). Aprobar o rechazar al empleado es el flujo del registro facial
  (`/enrollments/{id}/approve|reject`).

El archivo va cifrado al bucket, nunca a la base (`employee_document_service`). El ADMIN de la plataforma no tiene
endpoints aquí: no ve estos datos (regla 13).
"""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from app.core.config import settings
from app.core.exceptions import PayloadTooLargeError
from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CompanyScope,
    CompanyUser,
    CurrentUser,
    DbSession,
    EmployeeUser,
    Pagination,
    Trash,
    company_of,
    require_screen,
)
from app.i18n import Megabytes
from app.middleware.rate_limit import enforce
from app.models import Screen
from app.models.employee import Employee
from app.models.employee_document import DOCUMENT_TYPES
from app.schemas.common import ErrorResponse
from app.schemas.employee_document import (
    EmployeeDocumentData,
    EmployeeDocumentFile,
    EmployeeDocumentList,
    EmployeeDocumentRead,
    EmployeeDocumentRequirements,
)
from app.services.employee_document_service import DocumentUpload, EmployeeDocumentService

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "No autenticado"},
    403: {"model": ErrorResponse, "description": "Sin la pantalla o el rol"},
}
router = APIRouter(
    prefix="/me/documents",
    tags=["Documentos del empleado (EMPLOYEE)"],
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_DOCUMENTS))],
    responses=_ERRORS,
)
company_router = APIRouter(
    prefix="/validations/employees",
    tags=["Documentos del empleado (COMPANY)"],
    dependencies=[Depends(require_screen(Screen.COMPANY_VALIDATIONS))],
    responses=_ERRORS,
)

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "`DOCUMENT_NOT_FOUND` (o `EMPLOYEE_NOT_FOUND` del expediente)"}
}
_UPLOAD_ERRORS: dict[int | str, dict[str, Any]] = {
    413: {"model": ErrorResponse, "description": "`EMPLOYEE_DOCUMENT_TOO_LARGE`"},
    422: {
        "model": ErrorResponse,
        "description": "`DOCUMENT_TYPE_INVALID` (campo `type`), `DOCUMENT_EMPTY`, `DOCUMENT_FORMAT_NOT_ALLOWED`, "
        "`DOCUMENT_MACROS_NOT_ALLOWED`, `DOCUMENT_XML_UNSAFE`, `DOCUMENT_DAMAGED` o `DOCUMENT_IMAGE_TOO_LARGE` "
        "(campo `file`)",
    },
    429: {"model": ErrorResponse, "description": "`RATE_LIMIT_EMPLOYEE_DOCUMENT_UPLOADS_PER_MINUTE`"},
    503: {"model": ErrorResponse, "description": "`STORAGE_UNAVAILABLE` (nada cambia)"},
}
_UPLOAD_DESCRIPTION = (
    "Multipart: `file` (una foto del documento tomada con la cámara, o un archivo PDF/Word/Excel/XML/JPG/PNG de hasta "
    "`EMPLOYEE_DOCUMENT_MAX_MB`, reconocido por su CONTENIDO) y `type` (código de `employee_document_types`). Si el "
    "archivo es una imagen, el servidor extrae la información con OCR de MEJOR ESFUERZO (nunca bloquea por una lectura "
    "imperfecta) y la guarda para que la empresa la confirme o corrija. El archivo se guarda CIFRADO en el bucket "
    "(nunca en la base). Sin bucket: 503 `STORAGE_UNAVAILABLE` y nada cambia."
)
_FILE_DESCRIPTION = (
    "El archivo de un documento vigente en base64 (el contrato único de respuesta es JSON), con su nombre y su formato "
    "real. Se lee del bucket, se verifica y se descifra en memoria. Bucket caído: 503 `STORAGE_UNAVAILABLE`."
)


def _current_employee_id(user: EmployeeUser) -> int:
    """El id del empleo en la empresa de la sesión (garantizado por `EmployeeUser`: ya opera en una empresa)."""
    return cast(Employee, user.employee).id


CurrentEmployeeId = Annotated[int, Depends(_current_employee_id)]


def employee_document_upload(
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="Foto o archivo del documento")],
    type: Annotated[
        str, Form(min_length=1, max_length=30, description=f"Tipo de documento (catálogo {DOCUMENT_TYPES})")
    ],
) -> DocumentUpload:
    """El formulario de un documento nuevo, con el límite por persona: cada subida cuesta leerlo, cifrarlo y subirlo."""
    enforce(f"employee-document:user:{user.id}", settings.RATE_LIMIT_EMPLOYEE_DOCUMENT_UPLOADS_PER_MINUTE)
    limit = int(settings.EMPLOYEE_DOCUMENT_MAX_MB * 1024 * 1024)
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise PayloadTooLargeError(code="EMPLOYEE_DOCUMENT_TOO_LARGE", params={"size": Megabytes(limit)}, field="file")
    return DocumentUpload(type=type, file_name=file.filename, data=data)


Upload = Annotated[DocumentUpload, Depends(employee_document_upload)]


# ---------------------------------------------------------------- EMPLOYEE: los suyos


@router.get(
    "/requirements",
    response_model=ApiResponse[EmployeeDocumentRequirements],
    summary="EMPLOYEE: qué documentos pide la empresa y qué falta",
    description="Si la empresa exige documentos, los tipos por grupo (identificación oficial y comprobante de "
    "domicilio), lo que ya subió y qué le falta.",
)
def my_requirements(
    user: EmployeeUser, employee_id: CurrentEmployeeId, db: DbSession
) -> ApiResponse[EmployeeDocumentRequirements]:
    employee = cast(Employee, user.employee)
    result = EmployeeDocumentService(db, company_of(user)).requirements(
        employee_id, required=employee.company.require_employee_documents
    )
    return ok(result, code="EMPLOYEE_DOCUMENT_REQUIREMENTS")


@router.get(
    "",
    response_model=ApiResponse[EmployeeDocumentList],
    summary="EMPLOYEE: mis documentos (paginado, el más reciente primero)",
    description="`deleted=true`: la papelera («Eliminados»).",
)
def list_my_documents(
    user: EmployeeUser, employee_id: CurrentEmployeeId, db: DbSession, page: Pagination, deleted: Trash = False
) -> ApiResponse[EmployeeDocumentList]:
    result = EmployeeDocumentService(db, company_of(user)).list(employee_id, page, deleted=deleted)
    return ok(result, code="EMPLOYEE_DOCUMENTS", params={"count": result.total})


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[EmployeeDocumentRead],
    summary="EMPLOYEE: subir un documento (se procesa con OCR)",
    description=_UPLOAD_DESCRIPTION,
    responses=_UPLOAD_ERRORS,
)
def upload_my_document(
    upload: Upload, user: EmployeeUser, employee_id: CurrentEmployeeId, db: DbSession
) -> ApiResponse[EmployeeDocumentRead]:
    result = EmployeeDocumentService(db, company_of(user)).upload(
        employee_id, upload, by_employee=True, actor_email=user.email
    )
    return ok(result, code="EMPLOYEE_DOCUMENT_UPLOADED", status_code=201)


@router.get(
    "/{document_id}/file",
    response_model=ApiResponse[EmployeeDocumentFile],
    summary="EMPLOYEE: descargar un documento mío (en base64)",
    description=_FILE_DESCRIPTION,
    responses={**_NOT_FOUND, 503: {"model": ErrorResponse}},
)
def my_document_file(
    document_id: int, user: EmployeeUser, employee_id: CurrentEmployeeId, db: DbSession
) -> ApiResponse[EmployeeDocumentFile]:
    return ok(
        EmployeeDocumentService(db, company_of(user)).file(employee_id, document_id), code="EMPLOYEE_DOCUMENT_FILE"
    )


@router.delete(
    "/{document_id}",
    response_model=ApiResponse[None],
    summary="EMPLOYEE: eliminar un documento mío (a «Eliminados»; solo si la empresa no lo ha confirmado)",
    responses={
        **_NOT_FOUND,
        409: {"model": ErrorResponse, "description": "`ALREADY_DELETED` o `DOCUMENT_CONFIRMED`"},
    },
)
def delete_my_document(
    document_id: int, user: EmployeeUser, employee_id: CurrentEmployeeId, db: DbSession
) -> ApiResponse[None]:
    EmployeeDocumentService(db, company_of(user)).delete(employee_id, document_id, user.email)
    return ok(None, code="EMPLOYEE_DOCUMENT_DELETED")


@router.post(
    "/{document_id}/restore",
    response_model=ApiResponse[EmployeeDocumentRead],
    summary="EMPLOYEE: restaurar un documento mío",
    responses={**_NOT_FOUND, 409: {"model": ErrorResponse, "description": "`NOT_DELETED`"}},
)
def restore_my_document(
    document_id: int, user: EmployeeUser, employee_id: CurrentEmployeeId, db: DbSession
) -> ApiResponse[EmployeeDocumentRead]:
    return ok(
        EmployeeDocumentService(db, company_of(user)).restore(employee_id, document_id),
        code="EMPLOYEE_DOCUMENT_RESTORED",
    )


# ---------------------------------------------------------------- COMPANY: el expediente del empleado


@company_router.get(
    "/{employee_id}/documents",
    response_model=ApiResponse[EmployeeDocumentList],
    summary="COMPANY: documentos de un empleado (expediente)",
    description="`deleted=true`: la papelera. Los datos extraídos vienen en cada documento (editables).",
    responses=_NOT_FOUND,
)
def company_list_documents(
    employee_id: int, company: CompanyScope, db: DbSession, page: Pagination, deleted: Trash = False
) -> ApiResponse[EmployeeDocumentList]:
    result = EmployeeDocumentService(db, company).employee_list(employee_id, page, deleted=deleted)
    return ok(result, code="EMPLOYEE_DOCUMENTS", params={"count": result.total})


@company_router.get(
    "/{employee_id}/documents/{document_id}/file",
    response_model=ApiResponse[EmployeeDocumentFile],
    summary="COMPANY: descargar un documento del empleado (en base64)",
    description=_FILE_DESCRIPTION,
    responses={**_NOT_FOUND, 503: {"model": ErrorResponse}},
)
def company_document_file(
    employee_id: int, document_id: int, company: CompanyScope, db: DbSession
) -> ApiResponse[EmployeeDocumentFile]:
    return ok(
        EmployeeDocumentService(db, company).employee_file(employee_id, document_id), code="EMPLOYEE_DOCUMENT_FILE"
    )


@company_router.patch(
    "/{employee_id}/documents/{document_id}/data",
    response_model=ApiResponse[EmployeeDocumentRead],
    summary="COMPANY: confirmar o corregir los datos extraídos de un documento",
    description="Solo cambian los campos enviados (un valor vacío o null los borra). Queda registrado quién confirmó "
    "y cuándo.",
    responses=_NOT_FOUND,
)
def company_update_document_data(
    employee_id: int,
    document_id: int,
    payload: EmployeeDocumentData,
    reviewer: CompanyUser,
    company: CompanyScope,
    db: DbSession,
) -> ApiResponse[EmployeeDocumentRead]:
    result = EmployeeDocumentService(db, company).update_data(
        employee_id, document_id, payload, payload.model_fields_set, reviewer.email
    )
    return ok(result, code="EMPLOYEE_DOCUMENT_DATA_SAVED")
