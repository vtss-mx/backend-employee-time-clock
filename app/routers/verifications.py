"""Verificaciones de identidad de la empresa con su ubicación (pantalla «Verificaciones», mapa; COMPANY).

Solo lectura: la empresa ve DÓNDE y cuándo se verificó la identidad de su gente (empleado, validador y API), en un mapa
de Google Maps. La empresa sale de la sesión (`CompanyScope`); cada endpoint exige su pantalla (`COMPANY_VERIFICATIONS`)
y su rol. Nunca devuelve fotos del registro facial. Decisión del dueño del producto, 2026-10-07 (migración 0085).
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, DbSession, Pagination, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.verification import CompanyVerificationList
from app.services.company_verifications_service import CompanyVerificationsService

router = APIRouter(
    prefix="/verifications",
    tags=["Verificaciones (COMPANY)"],
    dependencies=[Depends(require_screen(Screen.COMPANY_VERIFICATIONS))],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Rol sin permisos"},
    },
)

StartFilter = Annotated[date | None, Query(description="Desde (YYYY-MM-DD, día de la hora del negocio)")]
EndFilter = Annotated[date | None, Query(description="Hasta (YYYY-MM-DD, inclusivo)")]
EmployeeFilter = Annotated[int | None, Query(gt=0, description="Solo las de ese empleado")]
SuccessFilter = Annotated[bool | None, Query(description="true solo exitosas, false solo fallidas; sin él, todas")]


@router.get(
    "",
    response_model=ApiResponse[CompanyVerificationList],
    summary="Verificaciones de la empresa con su ubicación (paginadas, la más reciente primero)",
    description=(
        "Para el mapa de «Verificaciones»: cada fila con la persona (y su foto), fecha y hora, resultado, método y, si "
        "la verificación llevó ubicación, su punto (`latitude`, `longitude`, `location_accuracy_m`; null si no la "
        "llevó). Filtros opcionales por empleado, rango de fechas y resultado. "
        "Nunca devuelve fotos del registro facial."
    ),
)
def list_verifications(
    company: CompanyScope,
    db: DbSession,
    page: Pagination,
    start: StartFilter = None,
    end: EndFilter = None,
    employee_id: EmployeeFilter = None,
    success: SuccessFilter = None,
) -> ApiResponse[CompanyVerificationList]:
    result = CompanyVerificationsService(db, company).list(
        page, start=start, end=end, employee_id=employee_id, success=success
    )
    return ok(result, code="VERIFICATIONS_LISTED", params={"count": result.total})
