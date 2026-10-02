"""Empresas (tenants) administradas por el ADMIN de la plataforma."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

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

    @field_validator("contact_email", "admin_email", check_fields=False)
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
    contact_email: EmailStr
    phone: PhoneNumber
    max_employees: int | None = Field(
        default=None, ge=1, le=1_000_000, description="Límite del plan (vacío = sin límite)"
    )
    admin_email: EmailStr = Field(description="Correo del administrador de la empresa (inicia sesión con él)")
    admin_password: str


class CompanyUpdate(_CompanyFields):
    """Actualización parcial."""

    name: str | None = Field(default=None, max_length=200)
    legal_name: str | None = Field(default=None, max_length=250)
    rfc: str | None = Field(default=None, max_length=20)
    contact_email: EmailStr | None = None
    phone: PhoneNumber | None = None
    max_employees: int | None = Field(default=None, ge=1, le=1_000_000)


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


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    legal_name: str | None = None
    rfc: str | None = None
    contact_email: str | None = None
    phone: str | None = None
    active: bool
    max_employees: int | None = None
    employee_count: int = 0
    admin_count: int = 0
    created_at: datetime
    updated_at: datetime


class CompanyDetail(CompanyRead):
    admins: list[CompanyAdminRead]


class CompanyList(BaseModel):
    items: list[CompanyRead]
    total: int
    page: int
    size: int


class PlatformStats(BaseModel):
    companies: int
    active_companies: int
    employees: int
    company_admins: int
