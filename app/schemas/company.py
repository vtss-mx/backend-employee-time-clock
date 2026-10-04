"""Empresas (tenants) administradas por el ADMIN de la plataforma."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.common import Page
from app.schemas.validators import (
    PhoneNumber,
    normalize_company_name,
    normalize_company_rfc,
    validate_password_strength,
)


class _CompanyFields(BaseModel):
    @field_validator("name", check_fields=False)
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else normalize_company_name(value, "El nombre comercial")

    @field_validator("legal_name", check_fields=False)
    @classmethod
    def _legal_name(cls, value: str | None) -> str | None:
        return None if value is None else normalize_company_name(value, "La razón social")

    @field_validator("rfc", check_fields=False)
    @classmethod
    def _rfc(cls, value: str | None) -> str | None:
        return None if value is None else normalize_company_rfc(value)

    @field_validator("admin_email", check_fields=False)
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return None if value is None else value.lower()

    @field_validator("admin_password", check_fields=False)
    @classmethod
    def _password(cls, value: str | None) -> str | None:
        return None if value is None else validate_password_strength(value)


class CompanyCreate(_CompanyFields):
    """Alta de empresa con su primer administrador (COMPANY), en una sola operación."""

    name: str = Field(max_length=200, description="Nombre comercial", examples=["Panificadora del Norte"])
    legal_name: str = Field(
        max_length=250, description="Razón social", examples=["Panificadora del Norte, S.A. de C.V."]
    )
    rfc: str = Field(
        max_length=20, description="RFC: 12 (persona moral) o 13 (persona física)", examples=["PNO120315AB1"]
    )
    phone: PhoneNumber
    max_employees: int | None = Field(
        default=None, ge=1, le=1_000_000, description="Límite del plan (vacío = sin límite)"
    )
    admin_email: EmailStr = Field(description="Correo del administrador de la empresa (inicia sesión con él)")
    admin_password: str
    api_enabled: bool = Field(default=False, description="Acceso al módulo de Integraciones (API)")


class CompanyUpdate(_CompanyFields):
    """Actualización parcial."""

    name: str | None = Field(default=None, max_length=200)
    legal_name: str | None = Field(default=None, max_length=250)
    rfc: str | None = Field(default=None, max_length=20)
    phone: PhoneNumber | None = None
    max_employees: int | None = Field(default=None, ge=1, le=1_000_000)
    api_enabled: bool | None = Field(default=None, description="Acceso al módulo de Integraciones (API)")


class CompanyStatusUpdate(BaseModel):
    active: bool


class CompanyAdminCreate(_CompanyFields):
    admin_email: EmailStr
    admin_password: str


class CompanyAdminPasswordReset(_CompanyFields):
    """Contraseña nueva que el administrador de la plataforma asigna a un administrador de empresa."""

    admin_password: str


class CompanyAdminRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    active: bool
    last_login_at: datetime | None = None
    created_at: datetime


class CompanyAdminList(Page[CompanyAdminRead]):
    """Página de administradores de una empresa."""


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    legal_name: str | None = None
    rfc: str | None = None
    phone: str | None = None
    active: bool
    max_employees: int | None = None
    #: Tiene el módulo de Integraciones (API).
    api_enabled: bool = False
    employee_count: int = 0
    admin_count: int = 0
    created_at: datetime
    updated_at: datetime


class CompanyDetail(CompanyRead):
    """Detalle de una empresa. Sus administradores se piden aparte, paginados (`GET .../admins`):
    el detalle nunca carga una lista sin límite."""


class CompanyList(Page[CompanyRead]):
    """Página de empresas de la plataforma."""


class PlatformStats(BaseModel):
    companies: int
    active_companies: int
    employees: int
    company_admins: int


class CompanyEmployeeRead(BaseModel):
    """Un empleado de una empresa visto por el ADMIN de la plataforma: su ficha de trabajo, de solo
    lectura, y cuánto aprendió de él el reconocimiento facial (lo administra el ADMIN). Sin datos
    fiscales (RFC, CURP, NSS), fecha de nacimiento, fotos ni plantillas."""

    id: int
    employee_number: str
    first_name: str
    last_name: str
    department_name: str | None = None
    email: str
    phone: str | None = None
    active: bool
    face_status: str
    #: Muestras que el reconocimiento aprendió de sus identificaciones seguras (solo cuántas y cuándo).
    face_learned_samples: int = 0
    face_last_learned_at: datetime | None = None


class CompanyEmployeeList(Page[CompanyEmployeeRead]):
    """Empleados de una empresa (por apellido), para el ADMIN de la plataforma."""
