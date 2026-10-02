from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import ValidatorMode, VerificationMethod
from app.schemas.user import UserCompanyInfo


class CheckpointProfile(BaseModel):
    """Configuración del validador autenticado (qué métodos usa y qué exige su empresa)."""

    id: int
    name: str
    mode: ValidatorMode
    company: UserCompanyInfo
    liveness_required: bool
    qr_enabled: bool


class CheckpointQrRequest(BaseModel):
    qr_content: str = Field(min_length=1, max_length=512, description="Texto leído del código QR del empleado")


class CheckpointEmployee(BaseModel):
    """De quién es el QR (paso 1 del modo QR_AND_FACE; el rostro decide en el paso 2)."""

    employee_id: int
    name: str
    employee_number: str


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
