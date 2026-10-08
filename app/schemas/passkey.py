"""Llaves de acceso (WebAuthn / passkeys; antifraude fase 3): registrar, listar, renombrar y revocar las propias, y
entrar con una. Las credenciales viajan en el formato JSON de WebAuthn (bytes en base64url), tal como las entrega el
navegador; el servidor las verifica con py_webauthn."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.auth import DeviceLocation, DeviceProof
from app.schemas.common import Page

#: Tope del JSON de una credencial (una attestation "none" pesa menos de 2 KB; una con certificados, unos 8 KB).
CREDENTIAL_MAX_BYTES = 32_768


class PasskeyRead(BaseModel):
    """Una llave registrada: nunca su llave pública ni el id de su credencial (la app no los necesita)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None = None
    #: Cómo se conecta el autenticador (`internal`, `hybrid`, `usb`...); informativo.
    transports: list[str] = []
    #: Sincronizada en la nube de la plataforma (iCloud, Google): sobrevive a perder el dispositivo.
    backed_up: bool = False

    @field_validator("transports", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [item for item in value.split(",") if item]
        return value or []


class PasskeyList(Page[PasskeyRead]):
    """Las llaves de la cuenta, la más antigua primero."""


class ChallengeOptions(BaseModel):
    """El reto SELLADO (`token`, de un solo uso) y las opciones para el navegador (`navigator.credentials`)."""

    token: str
    options: dict[str, Any]


class PasskeyRegistration(BaseModel):
    """Lo que devuelve `navigator.credentials.create` más el reto sellado que lo originó y el nombre de la llave."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=2000)
    name: str = Field(min_length=1, max_length=60)
    credential: dict[str, Any]

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str:
        return " ".join(value.split())


class PasskeyRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str:
        return " ".join(value.split())


class PasskeyLogin(BaseModel):
    """Entrar con una llave: lo que devuelve `navigator.credentials.get`, el reto sellado y lo mismo que el inicio de
    sesión con contraseña (recordar la cuenta, y las pruebas de un validador: ubicación y dispositivo)."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=2000)
    credential: dict[str, Any]
    remember: bool = False
    location: DeviceLocation | None = None
    device: DeviceProof | None = None
