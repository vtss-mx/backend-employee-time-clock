from datetime import datetime
from typing import Self

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from app.models.enums import VerificationMethod
from app.schemas.common import Page
from app.schemas.employee import EmployeeList, EmployeeRead
from app.schemas.validator import ValidatorList, ValidatorRead

#: La integración nunca recibe fotos (regla 13; decisión del dueño, 2026-10-06): el campo no se envía ni se documenta.
NoPhoto = SkipJsonSchema[str | None]


class IntegrationEmployee(EmployeeRead):
    """Un empleado para la integración: la ficha que ve la empresa, sin su foto de perfil."""

    avatar: NoPhoto = Field(default=None, exclude=True)


class IntegrationEmployeeList(Page[IntegrationEmployee]):
    """Página de empleados para la integración (sin fotos)."""

    @classmethod
    def of_list(cls, employees: EmployeeList) -> Self:
        return cls.model_validate(employees, from_attributes=True)


class IntegrationValidator(ValidatorRead):
    """Un validador para la integración: lo que ve la empresa, sin la foto de perfil de su cuenta."""

    avatar: NoPhoto = Field(default=None, exclude=True)


class IntegrationValidatorList(ValidatorList):
    """Página de validadores para la integración (sin fotos), con el uso de su límite."""

    items: list[IntegrationValidator]  # type: ignore[assignment]

    @classmethod
    def of_list(cls, validators: ValidatorList) -> Self:
        return cls.model_validate(validators, from_attributes=True)


def integration_employee(employee: EmployeeRead) -> IntegrationEmployee:
    return IntegrationEmployee.model_validate(employee, from_attributes=True)


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
    #: Identificador fiscal (migración 0074): país (ISO 3166-1 alfa-2), tipo (`catalog.tax_id_types`, p. ej. `MX_RFC`,
    #: `US_EIN`) y número normalizado (mayúsculas, sin espacios, guiones, puntos ni diagonales); null = sin capturar.
    tax_country: str | None = None
    tax_id_type: str | None = None
    tax_id: str | None = None
    rfc: str | None = Field(
        default=None,
        description="Obsoleto (se conserva por compatibilidad): usa `tax_id`. El número si el tipo es `MX_RFC`; si "
        "no, null",
        json_schema_extra={"deprecated": True},
    )
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
