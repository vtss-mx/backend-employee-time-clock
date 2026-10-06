"""Sitios de trabajo de la empresa (solo COMPANY, pantalla "Sitios de trabajo"): dónde se checa en sitio, su código de
sitio y los kioscos que lo muestran (antifraude 2b)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, CompanyUser, DbSession, Pagination, Trash, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.kiosk import KioskCreate, KioskCreated, KioskList, KioskRead
from app.schemas.shift import SiteCreate, SiteList, SiteRead, SiteStatusUpdate, SiteUpdate
from app.services.kiosk_service import KioskService
from app.services.site_service import SiteService

router = APIRouter(
    prefix="/sites",
    tags=["Sitios de trabajo (COMPANY)"],
    dependencies=[Depends(require_screen(Screen.COMPANY_SITES))],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Sitio no encontrado"}}
#: Restaurar (borrado lógico).
RESTORE: dict[int | str, dict[str, Any]] = {
    **NOT_FOUND,
    409: {"model": ErrorResponse, "description": "`NOT_DELETED` o `RESTORE_CONFLICT` (su nombre ya se usa)"},
}


@router.get(
    "",
    response_model=ApiResponse[SiteList],
    summary="Sitios de la empresa (paginado)",
    description="`deleted=true`: la papelera («Eliminados»), con quién y cuándo eliminó cada uno.",
)
def list_sites(
    _: CompanyUser,
    company_id: CompanyScope,
    db: DbSession,
    page: Pagination,
    search: Annotated[str | None, Query(max_length=100)] = None,
    active: bool | None = None,
    deleted: Trash = False,
) -> ApiResponse[SiteList]:
    result = SiteService(db, company_id).search(search=search, active=active, page=page, deleted=deleted)
    return ok(result, code="SITES", params={"count": result.total})


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[SiteRead],
    summary="Alta de un sitio (domicilio con su punto en el mapa y radio)",
    responses={409: {"model": ErrorResponse, "description": "Nombre ya usado"}},
)
def create_site(body: SiteCreate, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[SiteRead]:
    return ok(SiteService(db, company_id).create(body), code="SITE_CREATED", status_code=201)


@router.get(
    "/{site_id}",
    response_model=ApiResponse[SiteRead],
    summary="Un sitio (también uno en «Eliminados», con `deleted_at`)",
    responses=NOT_FOUND,
)
def get_site(site_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[SiteRead]:
    return ok(SiteService(db, company_id).read(site_id, include_deleted=True), code="SITE")


@router.put("/{site_id}", response_model=ApiResponse[SiteRead], summary="Editar un sitio", responses=NOT_FOUND)
def update_site(
    site_id: int, body: SiteUpdate, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[SiteRead]:
    return ok(SiteService(db, company_id).update(site_id, body), code="SITE_UPDATED")


@router.patch(
    "/{site_id}/status", response_model=ApiResponse[SiteRead], summary="Activar / desactivar", responses=NOT_FOUND
)
def set_site_status(
    site_id: int, body: SiteStatusUpdate, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[SiteRead]:
    site = SiteService(db, company_id).set_active(site_id, body.active)
    return ok(site, code="SITE_STATUS", key="SITE_ACTIVATED" if body.active else "SITE_DEACTIVATED")


@router.delete(
    "/{site_id}",
    response_model=ApiResponse[None],
    summary="Eliminar un sitio (a «Eliminados»; solo si ningún turno lo usa y nadie ha checado ahí)",
    description="Borrado lógico: su nombre queda libre y se puede restaurar durante `SOFT_DELETE_RETENTION_DAYS`.",
    responses={
        **NOT_FOUND,
        409: {
            "model": ErrorResponse,
            "description": (
                "`SITE_IN_USE` (con los turnos que lo usan en `details.shifts`), `SITE_HAS_RECORDS` o `ALREADY_DELETED`"
            ),
        },
    },
)
def delete_site(site_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[None]:
    SiteService(db, company_id).delete(site_id, user)
    return ok(None, code="SITE_DELETED")


@router.post(
    "/{site_id}/restore", response_model=ApiResponse[SiteRead], summary="Restaurar un sitio", responses=RESTORE
)
def restore_site(site_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession) -> ApiResponse[SiteRead]:
    return ok(SiteService(db, company_id).restore(site_id), code="SITE_RESTORED")


# ---------------------------------------------------------------- kioscos del sitio (antifraude 2b)

KIOSK_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "`SITE_NOT_FOUND` o `KIOSK_NOT_FOUND`"}
}


@router.get(
    "/{site_id}/kiosks",
    response_model=ApiResponse[KioskList],
    summary="Kioscos del sitio (paginado, el más reciente primero)",
    description=(
        "La tableta de cada kiosco muestra el código rotativo del sitio (antifraude 2b). `deleted=true`: la papelera "
        "(«Eliminados»)."
    ),
    responses=NOT_FOUND,
)
def list_kiosks(
    site_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession, page: Pagination, deleted: Trash = False
) -> ApiResponse[KioskList]:
    result = KioskService(db, company_id).list(site_id, page, deleted=deleted)
    return ok(result, code="KIOSKS", params={"count": result.total})


@router.post(
    "/{site_id}/kiosks",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[KioskCreated],
    summary="Alta de un kiosco del sitio (con su código de vinculación, que se entrega UNA sola vez)",
    description=(
        "Responde el código de vinculación (`pairing_code`, vence en `SITE_KIOSK_PAIRING_HOURS`): la tableta lo canjea "
        "en `/kiosk` con la llave de su dispositivo. En la base solo queda su SHA-256."
    ),
    responses=NOT_FOUND,
)
def create_kiosk(
    site_id: int, body: KioskCreate, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[KioskCreated]:
    return ok(KioskService(db, company_id).create(site_id, body), code="KIOSK_CREATED", status_code=201)


@router.post(
    "/{site_id}/kiosks/{kiosk_id}/pairing",
    response_model=ApiResponse[KioskCreated],
    summary="Código de vinculación nuevo (la tableta vinculada deja de mostrar el código)",
    responses=KIOSK_NOT_FOUND,
)
def new_kiosk_pairing(
    site_id: int, kiosk_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[KioskCreated]:
    return ok(KioskService(db, company_id).new_pairing(site_id, kiosk_id), code="KIOSK_PAIRING_RENEWED")


@router.delete(
    "/{site_id}/kiosks/{kiosk_id}",
    response_model=ApiResponse[None],
    summary="Eliminar un kiosco (a «Eliminados»: su tableta deja de mostrar el código)",
    responses={**KIOSK_NOT_FOUND, 409: {"model": ErrorResponse, "description": "`ALREADY_DELETED`"}},
)
def delete_kiosk(
    site_id: int, kiosk_id: int, user: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[None]:
    KioskService(db, company_id).delete(site_id, kiosk_id, user)
    return ok(None, code="KIOSK_DELETED")


@router.post(
    "/{site_id}/kiosks/{kiosk_id}/restore",
    response_model=ApiResponse[KioskRead],
    summary="Restaurar un kiosco (con su tableta vinculada, si la tenía)",
    responses={**KIOSK_NOT_FOUND, 409: {"model": ErrorResponse, "description": "`NOT_DELETED`"}},
)
def restore_kiosk(
    site_id: int, kiosk_id: int, _: CompanyUser, company_id: CompanyScope, db: DbSession
) -> ApiResponse[KioskRead]:
    return ok(KioskService(db, company_id).restore(site_id, kiosk_id), code="KIOSK_RESTORED")
