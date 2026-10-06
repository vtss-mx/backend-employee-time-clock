"""Empresas (tenants) administradas por el ADMIN de la plataforma."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_validator

from app.models.company import RFC_TAX_ID_TYPE, VALIDATORS_MAX
from app.schemas.billing import BillingPlanIn
from app.schemas.common import Deletion, Page
from app.schemas.tax_ids import TaxIdFields
from app.schemas.validators import PhoneNumber, normalize_company_name, validate_password_strength


class _CompanyFields(BaseModel):
    @field_validator("name", check_fields=False)
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else normalize_company_name(value, "TRADE_NAME_REQUIRED")

    @field_validator("legal_name", check_fields=False)
    @classmethod
    def _legal_name(cls, value: str | None) -> str | None:
        return None if value is None else normalize_company_name(value, "LEGAL_NAME_REQUIRED")

    @field_validator("admin_email", check_fields=False)
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return None if value is None else value.lower()

    @field_validator("admin_password", check_fields=False)
    @classmethod
    def _password(cls, value: str | None) -> str | None:
        return None if value is None else validate_password_strength(value)


class CompanyCreate(TaxIdFields, _CompanyFields):
    """Alta de empresa con su primer administrador (COMPANY), en una sola operación. Su identificador fiscal (país,
    tipo y número) es opcional: `TaxIdFields`."""

    name: str = Field(max_length=200, description="Nombre comercial", examples=["Panificadora del Norte"])
    legal_name: str = Field(
        max_length=250, description="Razón social", examples=["Panificadora del Norte, S.A. de C.V."]
    )
    phone: PhoneNumber
    max_employees: int | None = Field(
        default=None, ge=1, le=1_000_000, description="Límite del plan (vacío = sin límite)"
    )
    #: Opcional y con costo (cada validador activo cuenta como un empleado): por omisión la empresa no lo tiene.
    max_validators: int = Field(
        default=0, ge=0, le=VALIDATORS_MAX, description="Validadores activos permitidos (0 = sin el módulo)"
    )
    admin_email: EmailStr = Field(description="Correo del administrador de la empresa (inicia sesión con él)")
    admin_password: str
    api_enabled: bool = Field(default=False, description="Acceso al módulo de Integraciones (API)")
    #: Plan de cobro, en la misma operación (la webapp siempre lo envía). Sin él la empresa no se cobra
    #: hasta que el ADMIN se lo configure.
    billing: BillingPlanIn | None = None


class CompanyUpdate(TaxIdFields, _CompanyFields):
    """Actualización parcial: solo se modifican los campos enviados. Un null no cambia nada, salvo en el identificador
    fiscal (`tax_id` null o vacío lo borra con su país y su tipo) y en el límite de empleados, donde null lo borra (sin
    capturar · sin límite). El identificador cambia solo si llega `tax_id` (`TaxIdFields`)."""

    name: str | None = Field(default=None, max_length=200)
    legal_name: str | None = Field(default=None, max_length=250)
    phone: PhoneNumber | None = None
    max_employees: int | None = Field(default=None, ge=1, le=1_000_000, description="Null quita el límite")
    max_validators: int | None = Field(
        default=None,
        ge=0,
        le=VALIDATORS_MAX,
        description="Validadores activos permitidos (0 = sin el módulo). Nunca menos de los activos: 409 "
        "`VALIDATOR_LIMIT_BELOW_ACTIVE`",
    )
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


class CompanyRead(Deletion):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    legal_name: str | None = None
    #: Identificador fiscal (migración 0074): país (ISO 3166-1 alfa-2), tipo (`catalog.tax_id_types`) y número
    #: normalizado; los tres null = sin capturar.
    tax_country: str | None = None
    tax_id_type: str | None = None
    tax_id: str | None = None
    phone: str | None = None
    active: bool
    max_employees: int | None = None
    #: Validadores activos que puede tener (0 = sin el módulo de validadores).
    max_validators: int = 0
    #: Tiene el módulo de Integraciones (API).
    api_enabled: bool = False
    #: Estado de servicio (catalog.billing_statuses) y, si está suspendida, por qué (suspension_reasons).
    billing_status: str = "ACTIVE"
    suspension_reason: str | None = None
    employee_count: int = 0
    admin_count: int = 0
    #: Validadores activos (cuentan contra `max_validators` y en el cobro como empleados).
    active_validators: int = 0
    created_at: datetime
    updated_at: datetime

    @computed_field(description="Obsoleto: usa `tax_id`. El número si el tipo es RFC; si no, null")  # type: ignore[prop-decorator]
    @property
    def rfc(self) -> str | None:
        return self.tax_id if self.tax_id_type == RFC_TAX_ID_TYPE else None


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
