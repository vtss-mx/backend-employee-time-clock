from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.common import Page
from app.schemas.user import UserRead
from app.schemas.validators import validate_password_strength


class DeviceLocation(BaseModel):
    """Ubicación del dispositivo al iniciar sesión (la del navegador: GPS, Wi-Fi o red celular)."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0, le=100_000, description="Precisión (m), radio de 68 %")


class DeviceProof(BaseModel):
    """Prueba de posesión del dispositivo (validadores): su llave pública y la firma del reto."""

    public_key: str = Field(min_length=40, max_length=300, description="Llave pública ECDSA P-256 (SPKI DER, base64)")
    nonce: str = Field(min_length=10, max_length=200, description="Reto recibido en 403 DEVICE_PROOF_REQUIRED")
    signature: str = Field(
        min_length=40, max_length=200, description="Firma ECDSA P-256/SHA-256 del reto (r||s, base64)"
    )
    name: str | None = Field(default=None, max_length=120, description="Nombre del dispositivo (navegador y sistema)")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    #: "Recordar mi cuenta": mantener la sesión al cerrar el navegador (sin superar las 12 h).
    remember: bool = False
    #: Solo la piden los validadores que requieren ubicación (responden 403 `LOCATION_REQUIRED` sin ella).
    location: DeviceLocation | None = None
    #: Solo la piden los validadores de empresas que autorizan dispositivos (403 `DEVICE_PROOF_REQUIRED`).
    device: DeviceProof | None = None

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


class SessionList(Page[SessionRead]):
    """Página de las sesiones vigentes del usuario (la más reciente primero)."""


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
