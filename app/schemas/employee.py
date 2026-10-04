from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import FaceStatus
from app.schemas.common import Page
from app.schemas.validators import (
    PhoneNumber,
    normalize_curp,
    normalize_employee_number,
    normalize_name,
    normalize_nss,
    normalize_rfc,
    validate_birth_date,
    validate_password_strength,
)


class _EmployeeFields(BaseModel):
    @field_validator("first_name", "last_name", check_fields=False)
    @classmethod
    def _names(cls, value: str | None) -> str | None:
        return None if value is None else normalize_name(value)

    @field_validator("employee_number", check_fields=False)
    @classmethod
    def _number(cls, value: str | None) -> str | None:
        return None if value is None else normalize_employee_number(value)

    @field_validator("rfc", check_fields=False)
    @classmethod
    def _rfc(cls, value: str | None) -> str | None:
        return None if value is None else normalize_rfc(value)

    @field_validator("curp", check_fields=False)
    @classmethod
    def _curp(cls, value: str | None) -> str | None:
        return None if value is None else normalize_curp(value)

    @field_validator("nss", check_fields=False)
    @classmethod
    def _nss(cls, value: str | None) -> str | None:
        return None if value is None else normalize_nss(value)

    @field_validator("birth_date", check_fields=False)
    @classmethod
    def _birth(cls, value: date | None) -> date | None:
        return None if value is None else validate_birth_date(value)

    @field_validator("email", check_fields=False)
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return None if value is None else value.lower()

    @field_validator("password", check_fields=False)
    @classmethod
    def _password(cls, value: str | None) -> str | None:
        # Vacía = sin contraseña (persona que ya tiene cuenta, o edición sin cambiarla).
        return validate_password_strength(value) if value else None


class EmployeeCreate(_EmployeeFields):
    first_name: str = Field(max_length=100)
    last_name: str = Field(max_length=100)
    birth_date: date
    employee_number: str = Field(max_length=30)
    rfc: str = Field(max_length=20, description="RFC de persona física (13 caracteres)", examples=["PEGJ900515AB1"])
    curp: str = Field(max_length=25, description="CURP (18 caracteres)", examples=["HEGG560427MVZRRL04"])
    nss: str = Field(
        max_length=20, description="Número de Seguridad Social del IMSS (11 dígitos)", examples=["12345678903"]
    )
    phone: PhoneNumber
    email: EmailStr
    password: str | None = Field(
        default=None,
        description=(
            "Obligatoria para una persona nueva. Si la persona ya trabaja en otra empresa (mismo correo y "
            "teléfono), se vincula su cuenta y conserva su contraseña: este campo se ignora."
        ),
    )
    headwear_exempt: bool = False


class EmployeeUpdate(_EmployeeFields):
    """Actualización parcial: solo se modifican los campos enviados."""

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    birth_date: date | None = None
    employee_number: str | None = Field(default=None, max_length=30)
    rfc: str | None = Field(default=None, max_length=20)
    curp: str | None = Field(default=None, max_length=25)
    nss: str | None = Field(default=None, max_length=20)
    phone: PhoneNumber | None = None
    email: EmailStr | None = None
    password: str | None = None
    headwear_exempt: bool | None = None


class IdentityReverifyRequest(BaseModel):
    """Solicitud de COMPANY para que el empleado verifique nuevamente su identidad."""

    reason: str | None = Field(default=None, max_length=500, description="Motivo visible para el empleado (opcional)")


class IdentityReverifySummary(BaseModel):
    """Resultado de solicitar nueva verificación de identidad a toda la empresa."""

    employees: int = Field(description="Empleados con registro facial que deberán registrar su rostro de nuevo")


class EmployeeStatusUpdate(BaseModel):
    active: bool


class DepartmentRef(BaseModel):
    """Departamento (id y nombre) para mostrarlo junto al empleado."""

    id: int
    name: str


class EmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    employee_number: str
    first_name: str
    last_name: str
    full_name: str
    birth_date: date
    rfc: str | None = None
    curp: str | None = None
    nss: str | None = None
    phone: str | None = None
    email: EmailStr
    #: La persona también trabaja en otra empresa con la misma cuenta (correo, teléfono y
    #: contraseña no se pueden cambiar desde esta empresa).
    shared_account: bool = False
    active: bool
    headwear_exempt: bool
    face_status: FaceStatus
    face_rejection_reason: str | None = None
    latest_enrollment_id: int | None = None
    has_face: bool
    #: Muestras activas del rostro. Lo que el reconocimiento aprende del uso lo administra el ADMIN
    #: de la plataforma (`CompanyEmployeeRead`); la empresa no lo ve.
    face_samples: int
    #: Departamento al que está asignado (a lo más uno).
    department_id: int | None = None
    department_name: str | None = None
    #: Departamentos de los que es responsable (solo en el detalle).
    managed_departments: list[DepartmentRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EmployeeList(Page[EmployeeRead]):
    """Página de empleados de la empresa."""


class EmployeeIdList(BaseModel):
    """Los empleados de un filtro del listado ("seleccionar los N de este filtro" en una operación
    masiva): sus ids (a lo más `limit`, en el orden del listado) y cuántos coinciden en total."""

    ids: list[int]
    total: int
    #: Tope de una operación masiva: si `total` lo pasa, solo vienen los primeros.
    limit: int
