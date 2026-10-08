from datetime import datetime

from pydantic import BaseModel, Field

from app.core.config import settings
from app.models.enums import ValidatorMode, VerificationMethod
from app.schemas.auth import DeviceLocation
from app.schemas.capture import LocationSample
from app.schemas.common import Page
from app.schemas.user import UserCompanyInfo


class CheckpointProfile(BaseModel):
    """Configuración del validador autenticado (qué métodos usa y qué exige su empresa)."""

    id: int
    name: str
    mode: ValidatorMode
    company: UserCompanyInfo
    liveness_required: bool
    qr_enabled: bool
    #: Antifraude 2b: la app manda la ubicación en cada identificación (el validador la requiere y la empresa la
    #: revisa).
    location_required: bool = False
    #: Reto que firma la llave del dispositivo en la siguiente identificación (None si la empresa no pide firma).
    device_nonce: str | None = None


class SignedRequest(BaseModel):
    """La firma por petición del dispositivo del validador y su ubicación (antifraude 2b; todo opcional: lo que falte
    es una señal o, si la empresa lo exige, un 403). La firma es ECDSA P-256/SHA-256 (r||s, base64) de
    `"{signature_nonce}.{acción}.{SHA-256 del texto del QR}"`."""

    # Sin largo máximo aquí a propósito: una firma mal formada es una señal (o el 403 de la firma), nunca un 422
    # (`request_signing.RequestProof.bounded`); el cuerpo completo ya tiene su tope.
    signature_key: str | None = Field(default=None, description="Llave pública (SPKI DER, base64)")
    signature_nonce: str | None = Field(default=None, description="El `device_nonce` más reciente")
    signature: str | None = Field(default=None, description="Firma de la petición (r||s, base64)")
    location: DeviceLocation | None = Field(default=None, description="Ubicación del dispositivo ahora")
    location_samples: list[LocationSample] = Field(
        default_factory=list,
        max_length=settings.LOCATION_MAX_SAMPLES,
        description="Las lecturas de la ventana corta de la app (señales del lugar)",
    )


class CheckpointQrRequest(SignedRequest):
    qr_content: str = Field(min_length=1, max_length=512, description="Texto leído del código QR del empleado")


class CheckpointEmployee(BaseModel):
    """De quién es el QR (paso 1 del modo QR_AND_FACE; el rostro decide en el paso 2)."""

    employee_id: int
    name: str
    #: Opcional (migración 0076): null si el empleado no tiene número.
    employee_number: str | None = None
    #: Su foto de perfil (ruta versionada) o None: el validador es una cuenta de la empresa y ve a su gente.
    avatar: str | None = None
    #: Reto de la siguiente firma (antifraude 2b).
    device_nonce: str | None = None


class CheckpointEvent(BaseModel):
    """Una identificación hecha por el validador (bitácora en la BD)."""

    id: int
    created_at: datetime
    method: VerificationMethod
    success: bool
    reason: str | None = None
    confidence: float | None = None
    employee_name: str | None = None
    employee_number: str | None = None
    #: La foto de perfil de quien se identificó (ruta versionada) o None.
    avatar: str | None = None


class CheckpointEventList(Page[CheckpointEvent]):
    """Página de identificaciones del validador (la más reciente primero)."""
