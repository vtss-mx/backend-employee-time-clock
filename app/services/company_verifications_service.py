"""Verificaciones de identidad de una empresa con DÓNDE se hicieron (pantalla «Verificaciones», mapa de Google Maps;
decisión del dueño, 2026-10-07).

Solo lectura: lista la bitácora de UNA empresa (`verification_logs`) con la persona, el resultado, el método y el punto
donde ocurrió cada verificación (las columnas `latitude`/`longitude`/`location_accuracy_m` que llena `identity_core`
cuando la verificación llevó ubicación). La empresa SIEMPRE sale de la sesión (aislamiento por empresa, regla 14): el
`company_id` nunca llega del cliente. Reutiliza `VerificationLogRepository.page_for_company`
(su súper-índice resuelve el
periodo y el conteo con tope sin leer la tabla) y carga las personas por lotes (nunca una consulta por fila). Nunca
devuelve fotos del registro facial: solo la foto de PERFIL (`avatar`), que la empresa ve de su gente.
"""

from datetime import date, datetime

from sqlalchemy.orm import Session

from app.core.clock import business_day_bounds
from app.models import Employee, VerificationLog
from app.models.enums import VerificationMethod
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.verification_repository import VerificationLogRepository
from app.schemas.avatar import employee_avatar
from app.schemas.common import PageParams
from app.schemas.verification import CompanyVerificationList, CompanyVerificationRead


def _range(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    """Rango de fechas (días de la hora del negocio) en UTC, cerrado-abierto: `[inicio, fin)`.
    None donde no se filtró."""
    since = business_day_bounds(start)[0] if start is not None else None
    until = business_day_bounds(end)[1] if end is not None else None
    return since, until


def _row(log: VerificationLog, employee: Employee | None) -> CompanyVerificationRead:
    return CompanyVerificationRead(
        id=log.id,
        created_at=log.created_at,
        method=log.method,
        success=log.success,
        reason=log.reason,
        confidence=log.score if log.method != VerificationMethod.QR else None,
        employee_id=employee.id if employee else None,
        employee_number=employee.employee_number if employee else None,
        employee_name=employee.full_name if employee else None,
        avatar=employee_avatar(employee) if employee else None,
        latitude=log.latitude,
        longitude=log.longitude,
        location_accuracy_m=log.location_accuracy_m,
    )


class CompanyVerificationsService:
    """Las verificaciones de la empresa de la sesión, con su ubicación para el mapa."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def list(
        self,
        page: PageParams,
        *,
        start: date | None = None,
        end: date | None = None,
        employee_id: int | None = None,
        success: bool | None = None,
    ) -> CompanyVerificationList:
        since, until = _range(start, end)
        logs, total = VerificationLogRepository(self.db).page_for_company(
            self.company_id,
            since=since,
            until=until,
            employee_id=employee_id,
            success=success,
            offset=page.offset,
            limit=page.size,
        )
        people = EmployeeRepository(self.db, self.company_id).by_ids(
            {log.employee_id for log in logs if log.employee_id is not None}
        )
        rows = [_row(log, people.get(log.employee_id) if log.employee_id is not None else None) for log in logs]
        return CompanyVerificationList.of(rows, total, page)
