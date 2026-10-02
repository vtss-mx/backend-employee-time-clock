from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VerificationPolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    block_glasses: bool = Field(description="Exigir retirar lentes (incluye lentes de sol)")
    block_headwear: bool = Field(description="Exigir retirar gorra, sombrero o visera (salvo empleados exentos)")
    block_mask: bool = Field(description="Exigir retirar cubrebocas")
    liveness_challenge: bool = Field(description="Prueba de vida: girar la cabeza en una dirección aleatoria")
    anti_spoofing: bool = Field(description="Detectar fotos impresas, pantallas y videos (anti-spoofing pasivo)")
    qr_enabled: bool = Field(description="Permitir identificarse con el código QR")
    min_confidence: float = Field(description="Confianza mínima para aceptar el reconocimiento facial (0.80-0.99999)")
    employee_mobile_only: bool = Field(
        description="Los empleados solo pueden usar la aplicación desde un teléfono celular"
    )
    validator_mobile_only: bool = Field(
        default=True, description="Los validadores de identidad solo operan desde una tableta o un teléfono"
    )
    updated_at: datetime | None = None
    updated_by: str | None = None


class VerificationPolicyUpdate(BaseModel):
    """Actualización parcial: solo se modifican los campos enviados."""

    block_glasses: bool | None = None
    block_headwear: bool | None = None
    block_mask: bool | None = None
    liveness_challenge: bool | None = None
    anti_spoofing: bool | None = None
    qr_enabled: bool | None = None
    employee_mobile_only: bool | None = None
    validator_mobile_only: bool | None = None
    min_confidence: float | None = Field(default=None, ge=0.80, le=0.99999)
