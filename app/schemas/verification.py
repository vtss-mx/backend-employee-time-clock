from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.facial_recognition.pose import TurnDirection
from app.models.enums import VerificationMethod
from app.schemas.common import Page


class QrVerificationRequest(BaseModel):
    qr_content: str = Field(min_length=1, max_length=512, description="Texto leído del código QR")


class FaceChallengeResponse(BaseModel):
    """Reto de prueba de vida. Debe completarse antes de `expires_in` segundos."""

    liveness_required: bool
    challenge_id: str | None = None
    #: Primer giro (igual a `actions[0]`).
    action: TurnDirection | None = None
    instruction: str | None = None
    #: Todos los giros, en orden (uno o dos, según la empresa): una captura por giro en `challenge_image`.
    actions: list[TurnDirection] = []
    instructions: list[str] = []
    #: Giro mínimo esperado (ratio nariz/ojos) para guiar al usuario en el cliente.
    min_yaw_ratio: float | None = None
    expires_in: int | None = None


class VerificationResult(BaseModel):
    verified: bool
    method: VerificationMethod
    message: str
    employee_id: int | None = None
    employee_number: str | None = None
    name: str | None = None
    confidence: float | None = None
    verified_at: datetime | None = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "verified": True,
                    "method": "FACE",
                    "message": "Identificación exitosa",
                    "employee_id": 123,
                    "employee_number": "EMP-001",
                    "name": "Juan Perez",
                    "confidence": 0.94,
                    "verified_at": "2026-09-30T15:04:05Z",
                },
                {"verified": False, "method": "FACE", "message": "Rostro no reconocido"},
            ]
        }
    }


class VerificationLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    method: VerificationMethod
    success: bool
    score: float | None
    reason: str | None
    ip_address: str | None
    created_at: datetime


class VerificationLogList(Page[VerificationLogRead]):
    """Página de la bitácora de verificaciones de un empleado (la más reciente primero)."""
