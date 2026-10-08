"""Contrato de la API pública de verificación facial (SDK móviles; `docs/sdk/contrato-verificacion.md`).

El reto es el mismo de la aplicación web (`FaceChallengeResponse`) más lo que una aplicación nativa necesita saber para
capturar (`CaptureSpec`). El resultado de un intento es SIEMPRE un 200 con su decisión (permitir, en revisión, un paso
más o negar) y la referencia del empleado SIN foto: la API de integración nunca recibe fotos (regla 13 de la raíz).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import VerificationMethod
from app.schemas.common import Explained
from app.schemas.verification import FaceChallengeResponse


class ApiChallengeIn(BaseModel):
    """Quién pide el reto: la llave pública del dispositivo (SPKI DER de una llave P-256, base64). El `device_nonce` del
    reto queda ligado a ella y a la llave de la API."""

    model_config = ConfigDict(extra="forbid")

    device_key: str = Field(min_length=1, max_length=300, description="Llave pública P-256 (SPKI, base64)")


class CaptureSpec(BaseModel):
    """Cómo capturar (de la configuración del servidor): cuántas frontales, el tamaño de cada imagen y el tiempo humano
    mínimo antes de enviar (`FACE_CHALLENGE_MIN_SECONDS` por movimiento)."""

    frontal_min: int
    frontal_max: int
    frontal_recommended: int
    min_side_px: int
    max_side_px: int
    #: El lado con que el servidor procesa: más resolución solo pesa más.
    recommended_long_side_px: int
    max_image_bytes: int
    formats: list[str]
    min_response_seconds: float


class ApiChallenge(FaceChallengeResponse):
    """El reto de la API: el de siempre (sin destello: retirado y sin canal en vivo) más cómo capturar."""

    capture: CaptureSpec


#: Lo que decidió el servidor: coincide (`ALLOW`), coincide y la empresa lo revisa (`REVIEW`), el motor de riesgo pide
#: un paso más con el reto nuevo (`STEP_UP`) o no se confirmó (`DENY`, con su motivo).
type ApiDecision = Literal["ALLOW", "REVIEW", "STEP_UP", "DENY"]


class ApiEmployeeRef(BaseModel):
    """El empleado verificado o identificado: id, número y nombre. Nunca su foto."""

    id: int
    employee_number: str | None = None
    name: str


class ApiVerificationResult(Explained):
    """El resultado de un intento por la API pública (verificar 1:1 o identificar 1:N)."""

    #: La identidad se confirmó (`ALLOW` o `REVIEW`).
    matched: bool
    decision: ApiDecision
    #: Motivo de la bitácora (`verification_reasons` / `face_errors`) con `DENY`; `STEP_UP_REQUIRED` con `STEP_UP`.
    reason: str | None = None
    confidence: float | None = None
    employee: ApiEmployeeRef | None = None
    #: Id del intento en la bitácora: el servidor de la empresa lo confirma con su propia llave (`/attendance/feed`).
    attempt_id: int | None = None
    method: VerificationMethod = VerificationMethod.API_FACE
    verified_at: datetime | None = None
    #: Solo con `STEP_UP`: el reto nuevo (el SDK repite la captura con él, sin pedir otro).
    challenge: ApiChallenge | None = None
    message: str
