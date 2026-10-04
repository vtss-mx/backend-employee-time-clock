from datetime import datetime

from pydantic import BaseModel

from app.models.enums import VerificationMethod
from app.schemas.common import Page


class IntegrationKeyInfo(BaseModel):
    name: str
    prefix: str
    scopes: list[str]
    expires_at: datetime | None


class IntegrationCompanyRead(BaseModel):
    """La empresa dueña de la llave (la API solo da acceso a SU información) y la propia llave."""

    id: int
    name: str
    legal_name: str | None
    rfc: str | None
    #: Zona horaria del negocio: las fechas viajan en UTC (ISO 8601); los días se cuentan en esta zona.
    timezone: str
    key: IntegrationKeyInfo


class AttendanceEvent(BaseModel):
    """Una identificación de la bitácora (asistencia): quién, cuándo, cómo, dónde y el resultado."""

    id: int
    #: Momento de la identificación (UTC).
    occurred_at: datetime
    employee_id: int | None
    employee_number: str | None
    employee_name: str | None
    method: VerificationMethod
    success: bool
    #: Motivo del rechazo (catalog.verification_reasons); null si fue exitosa.
    reason: str | None
    confidence: float | None
    #: Validador (punto de control) que la hizo; null si fue el propio empleado o la empresa.
    validator_id: int | None
    validator_name: str | None


class AttendanceList(Page[AttendanceEvent]):
    """Página de la bitácora de identificaciones de la empresa (la más reciente primero)."""


class AttendanceFeed(BaseModel):
    """Tramo de la bitácora en orden cronológico para sincronizar: pide el siguiente con `next_cursor`."""

    items: list[AttendanceEvent]
    #: Cursor opaco del último elemento (envíalo como `after`); null si el tramo vino vacío.
    next_cursor: str | None
    #: Hay más registros después de este tramo (si es false, ya estás al día).
    has_more: bool
