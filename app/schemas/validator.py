from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import ValidatorMode
from app.schemas.validators import validate_password_strength

MODE_DESCRIPTION = (
    "QR: solo el código QR del empleado · FACE: solo rostro (con prueba de vida) · QR_OR_FACE: el "
    "operador elige · QR_AND_FACE: ambos (el rostro debe ser del dueño del QR)"
)


def _clean_name(value: str) -> str:
    name = " ".join(value.split())
    if len(name) < 2:
        raise ValueError("El nombre del validador es obligatorio")
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


class ValidatorCreate(_NameField, ValidatorPasswordReset):
    """Alta de un validador de identidad (su cuenta inicia sesión en una tableta o un teléfono)."""

    name: str = Field(max_length=120, description="Nombre o ubicación", examples=["Recepción planta 1"])
    email: EmailStr
    mode: ValidatorMode = Field(default=ValidatorMode.QR_OR_FACE, description=MODE_DESCRIPTION)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return value.lower()


class ValidatorUpdate(_NameField):
    """Cambio parcial del nombre o del modo de identificación."""

    name: str | None = Field(default=None, max_length=120)
    mode: ValidatorMode | None = Field(default=None, description=MODE_DESCRIPTION)


class ValidatorStatusUpdate(BaseModel):
    active: bool


class ValidatorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: EmailStr
    mode: ValidatorMode
    active: bool
    last_login_at: datetime | None = None
    #: Identificaciones exitosas de hoy (zona horaria del negocio).
    identifications_today: int = 0
    created_at: datetime
