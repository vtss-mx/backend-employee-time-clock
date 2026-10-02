from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.user import UserRead
from app.schemas.validators import validate_password_strength


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    #: "Recordar mi cuenta": mantener la sesión al cerrar el navegador (sin superar las 12 h).
    remember: bool = False

    @field_validator("email")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.lower()

    model_config = {"json_schema_extra": {"examples": [{"email": "admin@empresa.com", "password": "Admin1234"}]}}


class CompanySelection(BaseModel):
    """Empresa a la que entra un empleado que trabaja en varias."""

    company_id: int = Field(ge=1)


class RememberedAccountRead(BaseModel):
    """Cuenta recordada en este dispositivo: el login la muestra ya escrita."""

    email: EmailStr


class TokenResponse(BaseModel):
    """El refresh token NO viaja en el cuerpo: se entrega en una cookie HttpOnly."""

    access_token: str = Field(description="JWT ES256 (Authorization: Bearer ...)")
    token_type: str = "Bearer"  # noqa: S105 - esquema de autorización, no es un secreto
    expires_in: int = Field(description="Segundos de vigencia del access token (12 h por defecto)")
    expires_at: datetime
    session_id: str
    user: UserRead


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None
    current: bool = False


class Jwk(BaseModel):
    kty: str
    crv: str
    x: str
    y: str
    kid: str
    use: str
    alg: str


class JwksResponse(BaseModel):
    keys: list[Jwk]


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(description="Mín. 8 caracteres, con mayúscula, minúscula y número")

    @field_validator("new_password")
    @classmethod
    def _strong(cls, value: str) -> str:
        return validate_password_strength(value)
