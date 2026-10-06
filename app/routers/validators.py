"""Validadores de identidad de la empresa (rol COMPANY).

Módulo que concede el ADMIN: sin límite de validadores (`max_validators` = 0) la pantalla no aparece y TODA ruta de
aquí responde 403 `VALIDATORS_DISABLED` (`require_validators_module`)."""

from typing import Any

from fastapi import APIRouter, Depends, status

from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CompanyScope,
    CompanyUser,
    DbSession,
    Pagination,
    Trash,
    ValidatorsModule,
    require_screen,
    require_validators_module,
)
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.validator import (
    DeviceStatusUpdate,
    ValidatorCreate,
    ValidatorDeviceList,
    ValidatorDeviceRead,
    ValidatorList,
    ValidatorPasswordReset,
    ValidatorRead,
    ValidatorStatusUpdate,
    ValidatorUpdate,
)
from app.services.device_service import DeviceService
from app.services.validator_service import ValidatorService

router = APIRouter(
    dependencies=[Depends(require_screen(Screen.COMPANY_VALIDATORS)), Depends(require_validators_module)],
    prefix="/validators",
    tags=["Validadores de identidad (COMPANY)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Rol sin permisos o empresa sin el módulo (VALIDATORS_DISABLED)"},
    },
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Validador no encontrado"}}
#: Eliminar y restaurar (borrado lógico).
TRASH: dict[int | str, dict[str, Any]] = {
    **NOT_FOUND,
    409: {
        "model": ErrorResponse,
        "description": "`ALREADY_DELETED`, `NOT_DELETED`, `RESTORE_CONFLICT` o `VALIDATOR_LIMIT_REACHED`",
    },
}


@router.get(
    "",
    response_model=ApiResponse[ValidatorList],
    summary="Validadores de la empresa (paginado), con el uso de su límite",
    description=(
        "`active` = validadores activos; `limit` = los que permite el ADMIN (`max_validators`). `deleted=true`: la "
        "papelera («Eliminados»), con quién y cuándo eliminó a cada uno."
    ),
)
def list_validators(
    company: CompanyScope, module: ValidatorsModule, db: DbSession, page: Pagination, deleted: Trash = False
) -> ApiResponse[ValidatorList]:
    result = ValidatorService(db, company).list_validators(page, module.max_validators, deleted=deleted)
    return ok(result, code="VALIDATORS_LISTED", params={"count": result.total})


@router.get(
    "/{validator_id}",
    response_model=ApiResponse[ValidatorRead],
    summary="Detalle de un validador (también uno en «Eliminados», con `deleted_at`)",
    responses=NOT_FOUND,
)
def get_validator(validator_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[ValidatorRead]:
    service = ValidatorService(db, company)
    return ok(service.read(service.get(validator_id, include_deleted=True)), code="VALIDATOR_FOUND")


@router.post(
    "",
    response_model=ApiResponse[ValidatorRead],
    status_code=status.HTTP_201_CREATED,
    summary="Dar de alta un validador de identidad",
    description=(
        "Crea la cuenta (correo único en la plataforma) con la que el validador inicia sesión en una "
        "tableta o un teléfono, su modo (`QR`, `FACE`, `QR_OR_FACE` o `QR_AND_FACE`), el domicilio del "
        "acceso donde opera y, opcionalmente, **requiere ubicación**: solo inicia sesión a no más de "
        "`location_radius_m` metros del punto del domicilio (422 `LOCATION_POINT_REQUIRED` sin punto). Ocupa un "
        "lugar del límite de validadores activos (409 `VALIDATOR_LIMIT_REACHED` si no queda) y cuenta para el "
        "cobro como un empleado."
    ),
    responses={
        409: {"model": ErrorResponse, "description": "Correo ya registrado o límite de validadores alcanzado"},
        422: {"model": ErrorResponse, "description": "Datos inválidos o ubicación incompleta"},
    },
)
def create_validator(payload: ValidatorCreate, company: CompanyScope, db: DbSession) -> ApiResponse[ValidatorRead]:
    service = ValidatorService(db, company)
    validator = service.create(payload)
    return ok(service.read(validator), code="VALIDATOR_CREATED", status_code=201)


@router.put(
    "/{validator_id}",
    response_model=ApiResponse[ValidatorRead],
    summary="Editar nombre, modo, domicilio o ubicación exigida",
    description="Exigir ubicación o cambiar su punto o radio cierra las sesiones abiertas del validador.",
    responses=NOT_FOUND,
)
def update_validator(
    validator_id: int, payload: ValidatorUpdate, company: CompanyScope, db: DbSession
) -> ApiResponse[ValidatorRead]:
    service = ValidatorService(db, company)
    return ok(service.read(service.update(validator_id, payload)), code="VALIDATOR_UPDATED")


@router.patch(
    "/{validator_id}/status",
    response_model=ApiResponse[ValidatorRead],
    summary="Activar o desactivar (desactivar cierra sus sesiones)",
    description="Activar ocupa un lugar del límite de validadores activos: 409 `VALIDATOR_LIMIT_REACHED` si no queda.",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "Límite de validadores alcanzado"}},
)
def set_validator_status(
    validator_id: int, payload: ValidatorStatusUpdate, company: CompanyScope, db: DbSession
) -> ApiResponse[ValidatorRead]:
    service = ValidatorService(db, company)
    validator = service.set_active(validator_id, payload.active)
    return ok(service.read(validator), code="VALIDATOR_STATUS_UPDATED")


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
    return ok(service.read(validator), code="VALIDATOR_PASSWORD_RESET")


@router.delete(
    "/{validator_id}",
    response_model=ApiResponse[None],
    summary="Eliminar (a «Eliminados»; su historial de identificaciones se conserva y su foto se borra)",
    description=(
        "Borrado lógico de su configuración y su cuenta: deja de contar contra el límite y en el cobro desde hoy, "
        "sus sesiones se cierran y su correo queda libre. Se puede restaurar durante `SOFT_DELETE_RETENTION_DAYS`."
    ),
    responses=TRASH,
)
def delete_validator(validator_id: int, company: CompanyScope, user: CompanyUser, db: DbSession) -> ApiResponse[None]:
    ValidatorService(db, company).delete(validator_id, user)
    return ok(None, code="VALIDATOR_DELETED")


@router.post(
    "/{validator_id}/restore",
    response_model=ApiResponse[ValidatorRead],
    summary="Restaurar un validador de «Eliminados»",
    description=(
        "Revisa de nuevo que su correo esté libre (409 `RESTORE_CONFLICT`) y, si estaba activo, el límite de "
        "validadores activos (409 `VALIDATOR_LIMIT_REACHED`); vuelve a contar para el cobro desde hoy."
    ),
    responses=TRASH,
)
def restore_validator(validator_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[ValidatorRead]:
    service = ValidatorService(db, company)
    return ok(service.read(service.restore(validator_id)), code="VALIDATOR_RESTORED")


# ---------------- Dispositivos ----------------

DEVICE_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "No encontrado"}}


@router.get(
    "/{validator_id}/devices",
    response_model=ApiResponse[ValidatorDeviceList],
    summary="Dispositivos del validador (los por autorizar primero)",
    responses=DEVICE_NOT_FOUND,
)
def list_devices(
    validator_id: int, company: CompanyScope, db: DbSession, page: Pagination
) -> ApiResponse[ValidatorDeviceList]:
    ValidatorService(db, company).get(validator_id)
    result = DeviceService(db, company).list(validator_id, page)
    return ok(result, code="DEVICES_LISTED", params={"count": result.total})


@router.patch(
    "/{validator_id}/devices/{device_id}/status",
    response_model=ApiResponse[ValidatorDeviceRead],
    summary="Autorizar, rechazar o revocar un dispositivo",
    description=(
        "`APPROVED` autoriza (desde cualquier estado), `REJECTED` rechaza uno por autorizar y `REVOKED` "
        "retira la autorización de uno autorizado (cierra las sesiones del validador). Otro cambio: 409."
    ),
    responses={**DEVICE_NOT_FOUND, 409: {"model": ErrorResponse, "description": "Cambio no permitido"}},
)
def set_device_status(
    validator_id: int,
    device_id: int,
    payload: DeviceStatusUpdate,
    operator: CompanyUser,
    company: CompanyScope,
    db: DbSession,
) -> ApiResponse[ValidatorDeviceRead]:
    ValidatorService(db, company).get(validator_id)
    device = DeviceService(db, company).set_status(validator_id, device_id, payload.status, operator)
    return ok(device, code="DEVICE_STATUS_UPDATED")
