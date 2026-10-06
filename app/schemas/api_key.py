from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.i18n import LocalizedValueError
from app.models.enums import ApiKeyStatus
from app.schemas.common import Page


class ApiKeyCreate(BaseModel):
    """Llave nueva: nombre para reconocerla, permisos (catalog.api_scopes) y vigencia opcional."""

    name: str = Field(min_length=1, max_length=80, description="Para qué sistema es (p. ej. «Nómina»)")
    scopes: list[str] = Field(min_length=1, max_length=20, description="Permisos (códigos de catalog.api_scopes)")
    expires_in_days: int | None = Field(
        default=None, ge=1, le=730, description="Días de vigencia; null = sin vencimiento"
    )

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        name = " ".join(value.split())
        if not name:
            raise LocalizedValueError("API_KEY_NAME_REQUIRED")
        return name


class ApiKeyRead(BaseModel):
    """Llave sin su secreto (solo se ve al crearla o rotarla)."""

    id: int
    name: str
    #: Inicio de la llave, para reconocerla (p. ej. "tck_Ab3dE9fG").
    prefix: str
    scopes: list[str]
    status: ApiKeyStatus
    created_at: datetime
    created_by: str | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    last_used_ip: str | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None


class ApiKeyCreated(ApiKeyRead):
    """Llave recién creada o rotada: el secreto viaja UNA sola vez; guárdalo en un lugar seguro."""

    secret: str


class ApiKeyList(Page[ApiKeyRead]):
    """Página de llaves de la empresa (las más recientes primero)."""
