from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.i18n import LocalizedValueError, StoredText
from app.models.enums import DeviceStatus, ValidatorMode
from app.models.validator import LOCATION_RADIUS_MAX_M, LOCATION_RADIUS_MIN_M
from app.schemas.address import Address
from app.schemas.common import Deletion, Page
from app.schemas.validators import validate_password_strength

MODE_DESCRIPTION = (
    "QR: solo el código QR del empleado · FACE: solo rostro (con prueba de vida) · QR_OR_FACE: el "
    "operador elige · QR_AND_FACE: ambos (el rostro debe ser del dueño del QR)"
)


def _clean_name(value: str) -> str:
    name = " ".join(value.split())
    if len(name) < 2:
        raise LocalizedValueError("VALIDATOR_NAME_REQUIRED")
    return name


class _NameField(BaseModel):
    @field_validator("name", check_fields=False)
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _clean_name(value)


class ValidatorPasswordReset(BaseModel):
    """Contraseña nueva de la cuenta del validador (cierra sus sesiones abiertas)."""

    password: str

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        return validate_password_strength(value)


#: Radio permitido para iniciar sesión (m).
LocationRadius = Annotated[
    int,
    Field(
        ge=LOCATION_RADIUS_MIN_M,
        le=LOCATION_RADIUS_MAX_M,
        description="Radio (m) alrededor del punto del domicilio dentro del cual puede iniciar sesión",
        examples=[100],
    ),
]
LOCATION_REQUIRED_DESCRIPTION = (
    "Requiere ubicación: la cuenta solo inicia sesión a no más de `location_radius_m` metros del punto "
    "del domicilio (el dispositivo envía su ubicación al iniciar sesión)"
)


class ValidatorCreate(_NameField, ValidatorPasswordReset):
    """Alta de un validador de identidad (su cuenta inicia sesión en una tableta o un teléfono)."""

    name: str = Field(max_length=120, description="Nombre o ubicación", examples=["Recepción planta 1"])
    email: EmailStr
    mode: ValidatorMode = Field(default=ValidatorMode.QR_OR_FACE, description=MODE_DESCRIPTION)
    address: Address = Field(description="Domicilio del acceso donde opera (con su punto en el mapa)")
    location_required: bool = Field(default=False, description=LOCATION_REQUIRED_DESCRIPTION)
    location_radius_m: LocationRadius | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return value.lower()


class ValidatorUpdate(_NameField):
    """Cambio parcial: nombre, modo, domicilio (completo) y ubicación exigida al iniciar sesión."""

    name: str | None = Field(default=None, max_length=120)
    mode: ValidatorMode | None = Field(default=None, description=MODE_DESCRIPTION)
    address: Address | None = None
    location_required: bool | None = Field(default=None, description=LOCATION_REQUIRED_DESCRIPTION)
    location_radius_m: LocationRadius | None = None


class ValidatorStatusUpdate(BaseModel):
    active: bool


class ValidatorRead(Deletion):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: EmailStr
    mode: ValidatorMode
    active: bool
    last_login_at: datetime | None = None
    #: Identificaciones exitosas de hoy (zona horaria del negocio).
    identifications_today: int = 0
    #: None en los validadores dados de alta antes de pedir el domicilio.
    address: Address | None = None
    location_required: bool = False
    location_radius_m: int | None = None
    #: Dispositivos por autorizar y autorizados (Validadores › Dispositivos).
    devices_pending: int = 0
    devices_approved: int = 0
    created_at: datetime


class ValidatorList(Page[ValidatorRead]):
    """Página de validadores de la empresa, con el uso de su límite ("N de M")."""

    #: Validadores activos de la empresa (los que cuentan contra el límite y en el cobro).
    active: int = 0
    #: Validadores activos que permite el ADMIN (`companies.max_validators`).
    limit: int = 0


class ValidatorDeviceRead(BaseModel):
    """Dispositivo en el que inició sesión un validador (la empresa lo autoriza)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    #: El que dio el dispositivo o, si no dio uno, su tipo en el idioma de quien lee ("Tableta", "Tablet").
    name: StoredText
    user_agent: str | None = None
    status: DeviceStatus
    created_at: datetime
    last_seen_at: datetime | None = None
    last_ip: str | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None


class ValidatorDeviceList(Page[ValidatorDeviceRead]):
    """Página de dispositivos de un validador (los por autorizar primero)."""


class DeviceStatusUpdate(BaseModel):
    """Autorizar, rechazar (uno pendiente) o revocar (uno autorizado: cierra sus sesiones)."""

    status: Literal[DeviceStatus.APPROVED, DeviceStatus.REJECTED, DeviceStatus.REVOKED]
