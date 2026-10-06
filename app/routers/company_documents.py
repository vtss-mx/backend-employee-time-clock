"""Documentos de la empresa (decisión del dueño del producto, 2026-10-06): los archivos que la empresa y la plataforma
guardan para facturarle (constancia de situación fiscal, acta constitutiva, comprobante de domicilio...).

Dos routers con las MISMAS operaciones, cada uno con su pantalla y su rol (regla 2):

- `admin_router` (ADMIN, pantalla "Empresas"): `/admin/companies/{company_id}/documents`, desde la ficha de la empresa.
- `router` (COMPANY, pantalla "Documentos"): `/documents`, los de la empresa de la sesión.

Listar (paginado; `deleted=true`, la papelera), subir (multipart), descargar (base64 dentro del contrato), eliminar
(borrado lógico) y restaurar. El archivo va cifrado al bucket, nunca a la base (`company_document_service`).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from app.core.config import settings
from app.core.exceptions import PayloadTooLargeError
from app.core.responses import ApiResponse, ok
from app.dependencies import (
    AdminUser,
    CompanyScope,
    CompanyUser,
    CurrentUser,
    DbSession,
    Pagination,
    Trash,
    require_screen,
)
from app.i18n import Megabytes
from app.middleware.rate_limit import enforce
from app.models import Screen
from app.models.company_document import DOCUMENT_NOTE_MAX
from app.schemas.common import ErrorResponse
from app.schemas.company_document import CompanyDocumentFile, CompanyDocumentList, CompanyDocumentRead
from app.services.company_document_service import DOCUMENT_TYPES, CompanyDocumentService, DocumentUpload

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "No autenticado"},
    403: {"model": ErrorResponse, "description": "Sin la pantalla o el rol"},
}
admin_router = APIRouter(
    prefix="/admin/companies",
    tags=["Documentos de la empresa (ADMIN)"],
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
    responses=_ERRORS,
)
router = APIRouter(
    prefix="/documents",
    tags=["Documentos de la empresa (COMPANY)"],
    dependencies=[Depends(require_screen(Screen.COMPANY_DOCUMENTS))],
    responses=_ERRORS,
)

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "`DOCUMENT_NOT_FOUND` (o `COMPANY_NOT_FOUND` del ADMIN)"}
}
_UPLOAD_ERRORS: dict[int | str, dict[str, Any]] = {
    413: {"model": ErrorResponse, "description": "`DOCUMENT_TOO_LARGE`"},
    422: {
        "model": ErrorResponse,
        "description": "`DOCUMENT_TYPE_INVALID` (campo `type`), `DOCUMENT_EMPTY`, `DOCUMENT_FORMAT_NOT_ALLOWED`, "
        "`DOCUMENT_MACROS_NOT_ALLOWED`, `DOCUMENT_XML_UNSAFE`, `DOCUMENT_DAMAGED` o `DOCUMENT_IMAGE_TOO_LARGE` "
        "(campo `file`)",
    },
    429: {"model": ErrorResponse, "description": "`RATE_LIMIT_DOCUMENT_UPLOADS_PER_MINUTE`"},
    503: {"model": ErrorResponse, "description": "`STORAGE_UNAVAILABLE` (nada cambia)"},
}
_CHANGE_ERRORS: dict[int | str, dict[str, Any]] = {
    **_NOT_FOUND,
    403: {
        "model": ErrorResponse,
        "description": "`DOCUMENT_UPLOADED_BY_PLATFORM` (la empresa, en lo que subió el ADMIN)",
    },
}
_UPLOAD_DESCRIPTION = (
    "Multipart: `file` (PDF, Word DOC/DOCX, Excel XLS/XLSX, XML o imagen JPG/PNG de hasta `COMPANY_DOCUMENT_MAX_MB`, "
    "reconocido por su CONTENIDO), `type` (código de `company_document_types`) y `note` (opcional). Sin macros, XML "
    "sin DTD ni entidades, imágenes sin metadatos (EXIF con GPS) y el nombre limpio con la extensión de su formato "
    "real. Se guarda CIFRADO en el bucket (nunca en la base). Sin bucket: 503 `STORAGE_UNAVAILABLE` y nada cambia."
)
_FILE_DESCRIPTION = (
    "El archivo de un documento vigente en base64 (el contrato único de respuesta es JSON), con su nombre y su formato "
    "real. Se lee del bucket, se verifica y se descifra en memoria. Bucket caído: 503 `STORAGE_UNAVAILABLE`."
)


def read_document_upload(file: UploadFile) -> tuple[str | None, bytes]:
    """El nombre y el contenido del archivo, sin leer más de `COMPANY_DOCUMENT_MAX_MB` (413 `DOCUMENT_TOO_LARGE`)."""
    limit = int(settings.COMPANY_DOCUMENT_MAX_MB * 1024 * 1024)
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise PayloadTooLargeError(code="DOCUMENT_TOO_LARGE", params={"size": Megabytes(limit)}, field="file")
    return file.filename, data


def document_upload(
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="El archivo: PDF, DOC, DOCX, XLS, XLSX, XML, JPG o PNG")],
    type: Annotated[
        str, Form(min_length=1, max_length=30, description=f"Tipo de documento (catálogo {DOCUMENT_TYPES})")
    ],
    note: Annotated[str | None, Form(max_length=DOCUMENT_NOTE_MAX, description="Nota corta (opcional)")] = None,
) -> DocumentUpload:
    """El formulario de un documento nuevo, con el límite por persona (`RATE_LIMIT_DOCUMENT_UPLOADS_PER_MINUTE`): cada
    subida cuesta revisarlo, cifrarlo y subirlo al bucket."""
    enforce(f"document:user:{user.id}", settings.RATE_LIMIT_DOCUMENT_UPLOADS_PER_MINUTE)
    file_name, data = read_document_upload(file)
    return DocumentUpload(type=type, note=(note or "").strip() or None, file_name=file_name, data=data)


Upload = Annotated[DocumentUpload, Depends(document_upload)]


# ---------------------------------------------------------------- ADMIN: los de cualquier empresa


@admin_router.get(
    "/{company_id}/documents",
    response_model=ApiResponse[CompanyDocumentList],
    summary="Documentos de una empresa (paginado, el más reciente primero)",
    description="`deleted=true`: la papelera («Eliminados»), con quién y cuándo eliminó cada uno.",
    responses=_NOT_FOUND,
)
def admin_list_documents(
    company_id: int, user: AdminUser, db: DbSession, page: Pagination, deleted: Trash = False
) -> ApiResponse[CompanyDocumentList]:
    result = CompanyDocumentService(db, company_id, user).list(page, deleted=deleted)
    return ok(result, code="COMPANY_DOCUMENTS", params={"count": result.total})


@admin_router.post(
    "/{company_id}/documents",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[CompanyDocumentRead],
    summary="Subir un documento de la empresa",
    description=_UPLOAD_DESCRIPTION,
    responses={**_NOT_FOUND, **_UPLOAD_ERRORS},
)
def admin_upload_document(
    company_id: int, upload: Upload, user: AdminUser, db: DbSession
) -> ApiResponse[CompanyDocumentRead]:
    result = CompanyDocumentService(db, company_id, user).upload(upload)
    return ok(result, code="COMPANY_DOCUMENT_UPLOADED", status_code=201)


@admin_router.get(
    "/{company_id}/documents/{document_id}/file",
    response_model=ApiResponse[CompanyDocumentFile],
    summary="Descargar un documento de la empresa (en base64)",
    description=_FILE_DESCRIPTION,
    responses={**_NOT_FOUND, 503: {"model": ErrorResponse}},
)
def admin_document_file(
    company_id: int, document_id: int, user: AdminUser, db: DbSession
) -> ApiResponse[CompanyDocumentFile]:
    return ok(CompanyDocumentService(db, company_id, user).file(document_id), code="COMPANY_DOCUMENT_FILE")


@admin_router.delete(
    "/{company_id}/documents/{document_id}",
    response_model=ApiResponse[None],
    summary="Eliminar un documento de la empresa (a «Eliminados»)",
    responses={**_NOT_FOUND, 409: {"model": ErrorResponse, "description": "`ALREADY_DELETED`"}},
)
def admin_delete_document(company_id: int, document_id: int, user: AdminUser, db: DbSession) -> ApiResponse[None]:
    CompanyDocumentService(db, company_id, user).delete(document_id)
    return ok(None, code="COMPANY_DOCUMENT_DELETED")


@admin_router.post(
    "/{company_id}/documents/{document_id}/restore",
    response_model=ApiResponse[CompanyDocumentRead],
    summary="Restaurar un documento de la empresa",
    responses={**_NOT_FOUND, 409: {"model": ErrorResponse, "description": "`NOT_DELETED`"}},
)
def admin_restore_document(
    company_id: int, document_id: int, user: AdminUser, db: DbSession
) -> ApiResponse[CompanyDocumentRead]:
    result = CompanyDocumentService(db, company_id, user).restore(document_id)
    return ok(result, code="COMPANY_DOCUMENT_RESTORED")


# ---------------------------------------------------------------- COMPANY: los suyos


@router.get(
    "",
    response_model=ApiResponse[CompanyDocumentList],
    summary="Documentos de mi empresa (paginado, el más reciente primero)",
    description="`deleted=true`: la papelera («Eliminados»). `can_delete`: solo lo que subió la empresa.",
)
def list_documents(
    user: CompanyUser, company_id: CompanyScope, db: DbSession, page: Pagination, deleted: Trash = False
) -> ApiResponse[CompanyDocumentList]:
    result = CompanyDocumentService(db, company_id, user).list(page, deleted=deleted)
    return ok(result, code="COMPANY_DOCUMENTS", params={"count": result.total})


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[CompanyDocumentRead],
    summary="Subir un documento de mi empresa",
    description=_UPLOAD_DESCRIPTION,
    responses=_UPLOAD_ERRORS,
)
def upload_document(
    upload: Upload, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[CompanyDocumentRead]:
    result = CompanyDocumentService(db, company_id, user).upload(upload)
    return ok(result, code="COMPANY_DOCUMENT_UPLOADED", status_code=201)


@router.get(
    "/{document_id}/file",
    response_model=ApiResponse[CompanyDocumentFile],
    summary="Descargar un documento de mi empresa (en base64)",
    description=_FILE_DESCRIPTION,
    responses={**_NOT_FOUND, 503: {"model": ErrorResponse}},
)
def document_file(
    document_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[CompanyDocumentFile]:
    return ok(CompanyDocumentService(db, company_id, user).file(document_id), code="COMPANY_DOCUMENT_FILE")


@router.delete(
    "/{document_id}",
    response_model=ApiResponse[None],
    summary="Eliminar un documento de mi empresa (a «Eliminados»; solo los que subió la empresa)",
    responses={**_CHANGE_ERRORS, 409: {"model": ErrorResponse, "description": "`ALREADY_DELETED`"}},
)
def delete_document(document_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[None]:
    CompanyDocumentService(db, company_id, user).delete(document_id)
    return ok(None, code="COMPANY_DOCUMENT_DELETED")


@router.post(
    "/{document_id}/restore",
    response_model=ApiResponse[CompanyDocumentRead],
    summary="Restaurar un documento de mi empresa (solo los que subió la empresa)",
    responses={**_CHANGE_ERRORS, 409: {"model": ErrorResponse, "description": "`NOT_DELETED`"}},
)
def restore_document(
    document_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[CompanyDocumentRead]:
    return ok(CompanyDocumentService(db, company_id, user).restore(document_id), code="COMPANY_DOCUMENT_RESTORED")
