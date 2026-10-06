"""Kioscos de los sitios (antifraude 2b, decisión D9): la tableta que muestra el código rotativo de un sitio."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from app.i18n import StoredText
from app.models.site_kiosk import KIOSK_NAME_MAX
from app.schemas.common import Deletion, Page
from app.schemas.shift import Name


class KioskCreate(BaseModel):
    """Un kiosco nuevo del sitio: solo su nombre (dónde está la tableta)."""

    name: Annotated[Name, Field(max_length=KIOSK_NAME_MAX, examples=["Recepción planta 1"])]


class KioskRead(Deletion):
    """Un kiosco del sitio. `paired`: una tableta canjeó su código de vinculación y muestra el código del sitio."""

    id: int
    site_id: int
    name: str
    paired: bool
    paired_at: datetime | None = None
    #: El navegador de la tableta ("iPad · Safari") o su tipo en el idioma de quien lee ("Tableta", "Tablet").
    device_name: StoredText = None
    #: La última vez que pidió su código (se anota cada `SITE_KIOSK_SEEN_SECONDS`): si su tableta sigue encendida.
    last_seen_at: datetime | None = None
    #: Hasta cuándo vale su código de vinculación (None si ya se canjeó).
    pairing_expires_at: datetime | None = None
    created_at: datetime


class KioskList(Page[KioskRead]):
    """Kioscos de un sitio (el más reciente primero)."""


class KioskCreated(BaseModel):
    """El kiosco con su código de vinculación: se entrega UNA sola vez (en la base queda solo su SHA-256)."""

    kiosk: KioskRead
    pairing_code: str
    pairing_expires_at: datetime


class KioskPairIn(BaseModel):
    """La tableta canjea el código de vinculación con la llave pública de su dispositivo."""

    pairing_code: str = Field(min_length=1, max_length=40, description="Código de vinculación (XXXXX-XXXXX)")
    public_key: str = Field(min_length=40, max_length=300, description="Llave pública ECDSA P-256 (SPKI DER, base64)")
    name: str | None = Field(default=None, max_length=120, description="Nombre del dispositivo (navegador y sistema)")


class KioskSession(BaseModel):
    """La tableta quedó vinculada: de qué kiosco y sitio es y el reto que firma su primera petición del código."""

    kiosk_id: int
    site_name: str
    company_name: str
    device_nonce: str


class KioskCodeIn(BaseModel):
    """La tableta pide el código vigente con la firma de su llave sobre `"{nonce}.kiosk.{kiosk_id}"` (sin reto o con
    uno vencido, responde 403 `KIOSK_PROOF_REQUIRED` con uno nuevo en `details.nonce`)."""

    #: Un id de la tabla (INTEGER de PostgreSQL): fuera de su rango no puede existir (422, nunca un error de la base).
    kiosk_id: int = Field(gt=0, le=2_147_483_647)
    nonce: str | None = Field(default=None, max_length=200)
    signature: str | None = Field(default=None, max_length=200)


class KioskCode(BaseModel):
    """Lo que muestra el kiosco: el código del sitio (6 dígitos), su QR, cada cuánto cambia, cuántos segundos le quedan
    y el reto de la siguiente petición."""

    site_name: str
    company_name: str
    code: str
    qr: str
    period_seconds: int
    expires_in: int
    device_nonce: str
