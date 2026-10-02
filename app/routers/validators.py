"""Validadores de identidad de la empresa (rol COMPANY)."""

from typing import Any

from fastapi import APIRouter, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, DbSession
from app.schemas.common import ErrorResponse
from app.schemas.validator import (
    ValidatorCreate,
    ValidatorPasswordReset,
    ValidatorRead,
    ValidatorStatusUpdate,
    ValidatorUpdate,
)
from app.services.validator_service import ValidatorService

router = APIRouter(
    prefix="/validators",
    tags=["Validadores de identidad (COMPANY)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Rol sin permisos"},
    },
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Validador no encontrado"}}


@router.get("", response_model=ApiResponse[list[ValidatorRead]], summary="Validadores de la empresa")
def list_validators(company: CompanyScope, db: DbSession) -> ApiResponse[list[ValidatorRead]]:
    items = ValidatorService(db, company).list_validators()
    return ok(items, f"{len(items)} validador(es)", code="VALIDATORS_LISTED")


@router.post(
    "",
    response_model=ApiResponse[ValidatorRead],
    status_code=status.HTTP_201_CREATED,
    summary="Dar de alta un validador de identidad",
    description=(
        "Crea la cuenta (correo único en la plataforma) con la que el validador inicia sesión en una "
        "tableta o un teléfono, y su modo: `QR`, `FACE`, `QR_OR_FACE` o `QR_AND_FACE`."
    ),
    responses={409: {"model": ErrorResponse, "description": "Correo ya registrado"}},
)
def create_validator(payload: ValidatorCreate, company: CompanyScope, db: DbSession) -> ApiResponse[ValidatorRead]:
    service = ValidatorService(db, company)
    validator = service.create(payload)
    return ok(service.read(validator), "Validador registrado", code="VALIDATOR_CREATED", status_code=201)


@router.put(
    "/{validator_id}", response_model=ApiResponse[ValidatorRead], summary="Editar nombre o modo", responses=NOT_FOUND
)
def update_validator(
    validator_id: int, payload: ValidatorUpdate, company: CompanyScope, db: DbSession
) -> ApiResponse[ValidatorRead]:
    service = ValidatorService(db, company)
    return ok(service.read(service.update(validator_id, payload)), "Validador actualizado", code="VALIDATOR_UPDATED")


@router.patch(
    "/{validator_id}/status",
    response_model=ApiResponse[ValidatorRead],
    summary="Activar o desactivar (desactivar cierra sus sesiones)",
    responses=NOT_FOUND,
)
def set_validator_status(
    validator_id: int, payload: ValidatorStatusUpdate, company: CompanyScope, db: DbSession
) -> ApiResponse[ValidatorRead]:
    service = ValidatorService(db, company)
    validator = service.set_active(validator_id, payload.active)
    return ok(service.read(validator), "Validador actualizado", code="VALIDATOR_STATUS_UPDATED")


@router.put(
    "/{validator_id}/password",
    response_model=ApiResponse[ValidatorRead],
    summary="Restablecer su contraseña (cierra sus sesiones)",
    responses=NOT_FOUND,
)
def reset_validator_password(
    validator_id: int, payload: ValidatorPasswordReset, company: CompanyScope, db: DbSession
) -> ApiResponse[ValidatorRead]:
    service = ValidatorService(db, company)
    validator = service.reset_password(validator_id, payload)
    return ok(service.read(validator), "Contraseña restablecida", code="VALIDATOR_PASSWORD_RESET")


@router.delete(
    "/{validator_id}",
    response_model=ApiResponse[None],
    summary="Eliminar (su historial de identificaciones se conserva)",
    responses=NOT_FOUND,
)
def delete_validator(validator_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[None]:
    ValidatorService(db, company).delete(validator_id)
    return ok(None, "Validador eliminado", code="VALIDATOR_DELETED")
