from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.facial_recognition.pose import LivenessAction
from app.models.enums import VerificationMethod
from app.schemas.common import Page


class FaceChallengeResponse(BaseModel):
    """Reto de prueba de vida. Debe completarse antes de `expires_in` segundos.

    La app pinta la pantalla con cada color de `flash` (una captura por color, `flash_image`) y guía
    a la persona por cada movimiento de `actions` (una captura por movimiento, `challenge_image`)
    hasta los mínimos que se envían (los vigentes de la plataforma, que se calibran solos).
    """

    liveness_required: bool
    challenge_id: str | None = None
    #: Primer movimiento (igual a `actions[0]`).
    action: LivenessAction | None = None
    instruction: str | None = None
    #: Todos los movimientos, en orden (de uno a tres).
    actions: list[LivenessAction] = []
    instructions: list[str] = []
    #: Giro mínimo (ratio nariz/ojos), cambio mínimo al mirar arriba o abajo (pitch) y cuánto debe
    #: crecer el rostro al acercarse, para guiar a la persona en el cliente.
    min_yaw_ratio: float | None = None
    min_pitch_delta: float | None = None
    min_closer_scale: float | None = None
    #: Colores del destello en orden (#RRGGBB); vacío si la empresa no lo usa.
    flash: list[str] = []
    #: El destello es obligatorio (si no, se mide pero no bloquea).
    flash_required: bool = False
    expires_in: int | None = None


class ValidatorAttendance(BaseModel):
    """Lo que registró en la asistencia una identificación en un validador (si aplica)."""

    #: CHECK_IN o CHECK_OUT; None si no registró nada (sin turno ahora o doble lectura).
    action: str | None = None
    message: str


class VerificationResult(BaseModel):
    verified: bool
    method: VerificationMethod
    message: str
    employee_id: int | None = None
    employee_number: str | None = None
    name: str | None = None
    confidence: float | None = None
    verified_at: datetime | None = None
    #: Solo en un validador: la entrada o salida del turno que registró esta identificación.
    attendance: ValidatorAttendance | None = None

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
