"""Datos que la API de integración entrega a una llave: SOLO de la empresa de esa llave.

Cada consulta se construye con `client.company_id` (la empresa sale de la llave, nunca de la
petición) y con los repositorios ya aislados por empresa: un id de otra empresa responde 404.
Nunca se entregan fotos, plantillas faciales ni otros datos biométricos.
"""

import base64
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.exceptions import NotFoundError, UnprocessableError
from app.models import Company, VerificationLog
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.validator_repository import ValidatorRepository
from app.repositories.verification_repository import VerificationLogRepository
from app.schemas.common import PageParams
from app.schemas.integration import (
    AttendanceEvent,
    AttendanceFeed,
    AttendanceList,
    IntegrationCompanyRead,
    IntegrationKeyInfo,
)
from app.services.api_key_service import ApiClient

#: El recorrido por cursor deja fuera lo de los últimos segundos: una identificación cuya transacción
#: aún no confirma no puede quedar atrás del cursor (y perderse para siempre).
FEED_SETTLE_SECONDS = 60


class IntegrationService:
    def __init__(self, db: Session, client: ApiClient) -> None:
        self.db = db
        self.client = client

    def company(self) -> IntegrationCompanyRead:
        company = self.db.get(Company, self.client.company_id)
        if company is None:  # la llave se valida antes: solo si la empresa se borró a la mitad
            raise NotFoundError("Empresa no encontrada", code="COMPANY_NOT_FOUND")
        return IntegrationCompanyRead(
            id=company.id,
            name=company.name,
            legal_name=company.legal_name,
            rfc=company.rfc,
            timezone=settings.APP_TIMEZONE,
            key=IntegrationKeyInfo(
                name=self.client.name,
                prefix=self.client.prefix,
                scopes=sorted(self.client.scopes),
                expires_at=self.client.expires_at,
            ),
        )

    def attendance(
        self,
        page: PageParams,
        *,
        since: datetime | None,
        until: datetime | None,
        employee_id: int | None,
        success: bool | None,
    ) -> AttendanceList:
        """Bitácora de identificaciones de la empresa, con filtros por periodo, empleado y resultado."""
        if since and until and since >= until:
            raise UnprocessableError("«since» debe ser anterior a «until»", code="INVALID_RANGE", field="since")
        company_id = self.client.company_id
        employees = EmployeeRepository(self.db, company_id)
        if employee_id is not None and employees.get_by_id(employee_id) is None:
            raise NotFoundError("Empleado no encontrado", code="EMPLOYEE_NOT_FOUND")
        logs, total = VerificationLogRepository(self.db).page_for_company(
            company_id,
            since=since,
            until=until,
            employee_id=employee_id,
            success=success,
            offset=page.offset,
            limit=page.size,
        )
        events = self._events(logs, employees)
        return AttendanceList.of(events, total, page)

    def attendance_feed(
        self,
        *,
        after: str | None,
        until: datetime | None,
        employee_id: int | None,
        success: bool | None,
        limit: int,
    ) -> AttendanceFeed:
        """La bitácora en orden cronológico a partir de un cursor (sincronización incremental)."""
        company_id = self.client.company_id
        employees = EmployeeRepository(self.db, company_id)
        if employee_id is not None and employees.get_by_id(employee_id) is None:
            raise NotFoundError("Empleado no encontrado", code="EMPLOYEE_NOT_FOUND")
        logs = VerificationLogRepository(self.db).feed_for_company(
            company_id,
            after_id=decode_cursor(after) if after else None,
            settled_before=datetime.now(UTC) - timedelta(seconds=FEED_SETTLE_SECONDS),
            until=until,
            employee_id=employee_id,
            success=success,
            limit=limit + 1,
        )
        has_more = len(logs) > limit
        logs = logs[:limit]
        last = logs[-1] if logs else None
        return AttendanceFeed(
            items=self._events(logs, employees),
            next_cursor=encode_cursor(last.id) if last else after,
            has_more=has_more,
        )

    def _events(self, logs: list[VerificationLog], employees: EmployeeRepository) -> list[AttendanceEvent]:
        people = employees.by_ids({log.employee_id for log in logs if log.employee_id is not None})
        validators = ValidatorRepository(self.db, self.client.company_id).by_user_ids(
            {log.user_id for log in logs if log.user_id is not None}
        )
        events = []
        for log in logs:
            person = people.get(log.employee_id) if log.employee_id is not None else None
            validator = validators.get(log.user_id) if log.user_id is not None else None
            events.append(
                AttendanceEvent(
                    id=log.id,
                    occurred_at=as_utc(log.created_at),
                    employee_id=person.id if person else None,
                    employee_number=person.employee_number if person else None,
                    employee_name=person.full_name if person else None,
                    method=log.method,
                    success=log.success,
                    reason=log.reason,
                    confidence=log.score,
                    validator_id=validator.id if validator else None,
                    validator_name=validator.name if validator else None,
                )
            )
        return events


def encode_cursor(log_id: int) -> str:
    """Cursor opaco del último registro entregado (el cliente no depende de su formato)."""
    return base64.urlsafe_b64encode(f"v1:{log_id}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        version, log_id = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode().split(":", 1)
        if version != "v1":
            raise ValueError(version)
        return int(log_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UnprocessableError("El cursor no es válido", code="INVALID_CURSOR", field="after") from exc
