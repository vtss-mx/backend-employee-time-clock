"""Llaves de la API de integración de la empresa (rol COMPANY, pantalla Integraciones)."""

from typing import Any

from fastapi import APIRouter, Depends, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, CompanyUser, DbSession, Pagination, require_api_module, require_screen
from app.models import Screen
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyList, ApiKeyRead
from app.schemas.common import ErrorResponse
from app.services.api_key_service import ApiKeyService

router = APIRouter(
    dependencies=[Depends(require_screen(Screen.COMPANY_API)), Depends(require_api_module)],
    prefix="/api-keys",
    tags=["Integraciones: llaves de la API (COMPANY)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Rol sin permisos"},
    },
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Llave no encontrada"}}


@router.get("", response_model=ApiResponse[ApiKeyList], summary="Llaves de la empresa (paginado, sin secretos)")
def list_api_keys(company: CompanyScope, db: DbSession, page: Pagination) -> ApiResponse[ApiKeyList]:
    result = ApiKeyService(db, company).list_keys(page)
    return ok(result, code="API_KEYS_LISTED", params={"count": result.total})


@router.post(
    "",
    response_model=ApiResponse[ApiKeyCreated],
    status_code=status.HTTP_201_CREATED,
    summary="Crear una llave (el secreto se muestra UNA sola vez)",
    responses={409: {"model": ErrorResponse, "description": "Tope de llaves activas"}},
)
def create_api_key(
    payload: ApiKeyCreate, company: CompanyScope, user: CompanyUser, db: DbSession
) -> ApiResponse[ApiKeyCreated]:
    created = ApiKeyService(db, company).create(payload, user)
    return ok(created, code="API_KEY_CREATED", status_code=201)


@router.post(
    "/{key_id}/rotate",
    response_model=ApiResponse[ApiKeyCreated],
    status_code=status.HTTP_201_CREATED,
    summary="Rotar: llave nueva con los mismos permisos; la anterior deja de servir al instante",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "La llave ya estaba revocada"}},
)
def rotate_api_key(key_id: int, company: CompanyScope, user: CompanyUser, db: DbSession) -> ApiResponse[ApiKeyCreated]:
    created = ApiKeyService(db, company).rotate(key_id, user)
    return ok(created, code="API_KEY_ROTATED", status_code=201)


@router.delete(
    "/{key_id}",
    response_model=ApiResponse[ApiKeyRead],
    summary="Revocar una llave (deja de servir al instante; queda en el historial)",
    responses=NOT_FOUND,
)
def revoke_api_key(key_id: int, company: CompanyScope, user: CompanyUser, db: DbSession) -> ApiResponse[ApiKeyRead]:
    return ok(ApiKeyService(db, company).revoke(key_id, user), code="API_KEY_REVOKED", key="API_KEY_REVOKED_DONE")
