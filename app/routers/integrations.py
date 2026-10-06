"""API de integración: los sistemas de una empresa (nómina, ERP...) consultan SU información.

Se autentica SOLO con la llave de la empresa (cabecera `X-API-Key`) y cada endpoint exige su
permiso de lectura (catalog.api_scopes). La empresa sale de la llave: un id de otra empresa
responde 404. No acepta la sesión de un usuario (ni el resto de la API acepta la llave).
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.responses import ApiResponse, ok
from app.dependencies import ApiClientDep, DbSession, Pagination, require_api_scope, require_validators_api
from app.models import ApiScope
from app.schemas.common import ErrorResponse
from app.schemas.employee import EmployeeList, EmployeeRead
from app.schemas.integration import AttendanceFeed, AttendanceList, IntegrationCompanyRead
from app.schemas.validator import ValidatorList
from app.services.employee_service import EmployeeService
from app.services.integration_service import IntegrationService
from app.services.validator_service import ValidatorService

router = APIRouter(
    prefix="/integrations/v1",
    tags=["API de integración (llave de la empresa)"],
    responses={
        401: {"model": ErrorResponse, "description": "Falta la llave, no es válida, venció o se revocó"},
        403: {"model": ErrorResponse, "description": "La llave no tiene el permiso o la empresa está desactivada"},
        429: {"model": ErrorResponse, "description": "Demasiadas peticiones con esta llave"},
    },
)
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "No existe en tu empresa"}}

EmployeesReader = Annotated[ApiClientDep, Depends(require_api_scope(ApiScope.EMPLOYEES_READ))]
AttendanceReader = Annotated[ApiClientDep, Depends(require_api_scope(ApiScope.ATTENDANCE_READ))]
#: Además del permiso, la empresa debe tener el módulo de validadores (403 VALIDATORS_DISABLED).
ValidatorsReader = Annotated[ApiClientDep, Depends(require_validators_api)]


@router.get("/company", response_model=ApiResponse[IntegrationCompanyRead], summary="Tu empresa y tu llave")
def company(client: ApiClientDep, db: DbSession) -> ApiResponse[IntegrationCompanyRead]:
    return ok(IntegrationService(db, client).company(), code="INTEGRATION_COMPANY")


@router.get("/employees", response_model=ApiResponse[EmployeeList], summary="Empleados (paginado)")
def employees(
    client: EmployeesReader,
    db: DbSession,
    page: Pagination,
    search: Annotated[str | None, Query(max_length=100, description="Nombre, número, RFC, CURP o correo")] = None,
    active: Annotated[bool | None, Query(description="Filtrar por estado")] = None,
) -> ApiResponse[EmployeeList]:
    result = EmployeeService(db, client.company_id).list_employees(search=search, active=active, page=page)
    return ok(result, code="EMPLOYEES_LISTED", params={"count": result.total})


@router.get(
    "/employees/{employee_id}", response_model=ApiResponse[EmployeeRead], summary="Un empleado", responses=NOT_FOUND
)
def employee(employee_id: int, client: EmployeesReader, db: DbSession) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, client.company_id)
    return ok(service.read(service.get(employee_id)), code="EMPLOYEE_FOUND")


@router.get(
    "/attendance",
    response_model=ApiResponse[AttendanceList],
    summary="Bitácora de identificaciones (asistencia), la más reciente primero",
    responses=NOT_FOUND,
)
def attendance(
    client: AttendanceReader,
    db: DbSession,
    page: Pagination,
    since: Annotated[datetime | None, Query(description="Desde (incluido), ISO 8601")] = None,
    until: Annotated[datetime | None, Query(description="Hasta (excluido), ISO 8601")] = None,
    employee_id: Annotated[int | None, Query(description="Solo este empleado")] = None,
    success: Annotated[bool | None, Query(description="Solo exitosas (true) o fallidas (false)")] = None,
) -> ApiResponse[AttendanceList]:
    result = IntegrationService(db, client).attendance(
        page, since=since, until=until, employee_id=employee_id, success=success
    )
    return ok(result, code="ATTENDANCE_LISTED", params={"count": result.total})


@router.get(
    "/attendance/feed",
    response_model=ApiResponse[AttendanceFeed],
    summary="Bitácora en orden cronológico por cursor (sincronización incremental, sin OFFSET)",
    description=(
        "Para mantener al día otro sistema: la primera vez sin `after`; después, con el `next_cursor` "
        "recibido. Cada tramo cuesta lo mismo aunque haya millones de registros y no se salta ni repite "
        "registros aunque lleguen nuevos a la mitad. `has_more=false`: ya estás al día."
    ),
    responses={**NOT_FOUND, 422: {"model": ErrorResponse, "description": "Cursor no válido"}},
)
def attendance_feed(
    client: AttendanceReader,
    db: DbSession,
    after: Annotated[str | None, Query(max_length=200, description="Cursor `next_cursor` del tramo anterior")] = None,
    until: Annotated[datetime | None, Query(description="Hasta (excluido), ISO 8601")] = None,
    employee_id: Annotated[int | None, Query(description="Solo este empleado")] = None,
    success: Annotated[bool | None, Query(description="Solo exitosas (true) o fallidas (false)")] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Registros por tramo (máximo 500)")] = 100,
) -> ApiResponse[AttendanceFeed]:
    feed = IntegrationService(db, client).attendance_feed(
        after=after, until=until, employee_id=employee_id, success=success, limit=limit
    )
    return ok(feed, code="ATTENDANCE_FEED", params={"count": len(feed.items)})


@router.get(
    "/validators",
    response_model=ApiResponse[ValidatorList],
    summary="Validadores de identidad (paginado)",
    description="Con el uso del límite (`active`, `limit`). Sin el módulo de validadores: 403 `VALIDATORS_DISABLED`.",
)
def validators(client: ValidatorsReader, db: DbSession, page: Pagination) -> ApiResponse[ValidatorList]:
    result = ValidatorService(db, client.company_id).list_validators(page, client.max_validators)
    return ok(result, code="VALIDATORS_LISTED", params={"count": result.total})
