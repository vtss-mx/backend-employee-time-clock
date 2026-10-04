"""Catálogos de la base de datos (esquema catalog) para cualquier usuario autenticado."""

from fastapi import APIRouter

from app.core.responses import ApiResponse, ok
from app.dependencies import CurrentUser
from app.schemas.catalog import CatalogsRead
from app.schemas.common import ErrorResponse
from app.services.catalog_service import get_catalogs

router = APIRouter(prefix="/catalogs", tags=["Catálogos"], responses={401: {"model": ErrorResponse}})


@router.get(
    "",
    response_model=ApiResponse[CatalogsRead],
    summary="Todos los catálogos",
    description=(
        "Roles, métodos de identificación, modos de validador (con sus métodos), estados del registro "
        "facial y de las solicitudes, motivos de rechazo, accesorios, países con su lada, motivos "
        "sugeridos (rechazo y nuevo registro facial), niveles de confianza, errores de la captura facial "
        "(con su mensaje y si se puede reintentar) y marcas de revisión del registro facial. Viven en la base de datos "
        "(esquema `catalog`); incluye los inactivos para nombrar registros históricos."
    ),
)
def list_catalogs(_: CurrentUser) -> ApiResponse[CatalogsRead]:
    catalogs = get_catalogs()
    entries = dict(catalogs.entries)
    modes = [
        {**mode, "methods": list(catalogs.mode_methods.get(mode["code"], ()))} for mode in entries["validator_modes"]
    ]
    data = CatalogsRead.model_validate({**entries, "validator_modes": modes})
    return ok(data, "Catálogos", code="CATALOGS")
