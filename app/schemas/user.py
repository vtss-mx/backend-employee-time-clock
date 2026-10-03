from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field

from app.core.config import settings
from app.models.enums import FaceStatus, UserRole


class UserEmployeeInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_number: str
    rfc: str | None = None
    curp: str | None = None
    nss: str | None = None
    phone: str | None = None
    first_name: str
    last_name: str
    full_name: str
    active: bool
    headwear_exempt: bool
    face_status: FaceStatus
    face_rejection_reason: str | None = None


class UserCompanyInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    active: bool


class UserMembership(BaseModel):
    """Un empleo de la persona (una empresa en la que trabaja)."""

    model_config = ConfigDict(from_attributes=True)

    #: Id del empleado en esa empresa.
    id: int
    company: UserCompanyInfo
    active: bool
    face_status: FaceStatus


class UserPreferences(BaseModel):
    """Preferencias de la interfaz guardadas en la BD (siguen al usuario en cualquier dispositivo)."""

    # Claves antiguas o desconocidas guardadas en la BD se ignoran al leer.
    model_config = ConfigDict(extra="ignore")

    #: Menú lateral contraído en escritorio.
    sidebar_collapsed: bool = False


class UserPreferencesUpdate(BaseModel):
    """Cambio parcial: solo se modifican las preferencias enviadas."""

    model_config = ConfigDict(extra="forbid")

    sidebar_collapsed: bool | None = None


class ScreenRead(BaseModel):
    """Pantalla del usuario: el frontend arma con ellas su menú y sus rutas."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    #: Etiqueta corta: título de la barra superior en teléfonos (si no hay, `name`).
    short_name: str | None = None
    #: Ruta base de la pantalla.
    path: str
    #: Nombre del ícono (lucide).
    icon: str
    #: Contador que acompaña la opción (p. ej. PENDING_ENROLLMENTS).
    badge: str | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    #: Teléfono de la persona (E.164); único en la plataforma.
    phone: str | None = None
    role: UserRole
    active: bool
    last_login_at: datetime | None = None
    created_at: datetime
    employee: UserEmployeeInfo | None = None
    #: Empresa en la que opera: la suya (COMPANY) o la elegida en la sesión (EMPLOYEE). None
    #: para ADMIN o para un empleado que trabaja en varias empresas y aún no elige.
    company: UserCompanyInfo | None = Field(default=None, validation_alias="current_company")
    #: Empresas en las que trabaja la persona (EMPLOYEE). Con varias, elige una al iniciar sesión.
    memberships: list[UserMembership] = Field(default_factory=list, validation_alias="employees")
    preferences: UserPreferences = Field(default_factory=UserPreferences)
    #: Pantallas del usuario en orden (menú y rutas del frontend). Las arma navigation_service.
    screens: list[ScreenRead] = Field(default_factory=list)
    #: Zona horaria del negocio (hora del Centro): la webapp muestra fechas y horas en ella, no en
    #: la del dispositivo (un teléfono en otra zona ve la misma hora que la empresa).
    timezone: str = Field(default_factory=lambda: settings.APP_TIMEZONE)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def home(self) -> str | None:
        """Inicio del usuario: su primera pantalla."""
        return self.screens[0].path if self.screens else None
