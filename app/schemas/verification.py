from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.facial_recognition.pose import LivenessAction
from app.models.enums import VerificationMethod
from app.schemas.common import Explained, Page


class BurstSpec(BaseModel):
    """La ráfaga corta de recortes del rostro que el servidor pide con el reto (antifraude 2a, decisión D11): lado de
    cada recorte (px), cuántos del tramo quieto (de frente) y del de movimiento (el primer giro o cabeceo), cuadros por
    segundo, calidad JPEG de la hoja, cuánto más grande que el rostro es la zona recortada, su tamaño máximo (bytes) y
    los recortes mínimos para analizarla (con menos, la app no la manda). Nunca se guarda: se analiza en memoria y se
    descarta."""

    tile: int
    hold: int
    move: int
    fps: int
    quality: float
    margin: float
    max_bytes: int
    min_frames: int


class FlashPace(BaseModel):
    """El destello dictado por el servidor (antifraude 2a): los colores NO viajan con el reto. La app manda `token` por
    el canal en vivo (`{"type": "flash", "token": ...}`) y recibe cada color con el token siguiente; responde cada uno
    con la huella SHA-256 de su captura dentro de `window_ms`. Sin canal, pide los colores de siempre con
    `POST /api/face/challenge/flash` (y el intento se marca como no dictado)."""

    token: str
    #: Colores que se dictarán.
    total: int
    window_ms: int


#: Para qué se pide el reto: una verificación (los 1 a 3 movimientos de la política) o el registro facial (siempre
#: los cuatro movimientos de la cabeza; decisión del dueño, 2026-10-07).
type ChallengePurpose = Literal["VERIFICATION", "ENROLLMENT"]


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
    #: Todos los movimientos, en orden: de uno a tres en una verificación; los cuatro de la cabeza en el registro
    #: (`purpose=ENROLLMENT`), con la vuelta al frente entre uno y otro.
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
    #: Reto de "un paso más" (el motor de riesgo lo pidió): el máximo de movimientos y el destello obligatorio.
    step_up: bool = False
    #: Reto que firma la llave del dispositivo del empleado y vuelve con las capturas (`device_nonce`,
    #: `device_signature`, `device_key`); None si su empresa no vincula dispositivos (decisión D2).
    device_nonce: str | None = None
    #: Antifraude 2a: el destello dictado por el servidor (entonces `flash` va vacío) y la ráfaga que se pide con las
    #: capturas; None si la empresa no los usa.
    flash_pace: FlashPace | None = None
    burst: BurstSpec | None = None


class FlashTokenIn(BaseModel):
    """El token inicial del destello dictado (para pedir los colores de siempre cuando no hay canal en vivo)."""

    token: str = Field(min_length=1, max_length=4096)


class FlashColors(BaseModel):
    """Los colores del destello de siempre (#RRGGBB, en orden): el respaldo sin canal en vivo."""

    flash: list[str]


class ValidatorAttendance(BaseModel):
    """Lo que registró en la asistencia una identificación en un validador (si aplica)."""

    #: CHECK_IN o CHECK_OUT; None si no registró nada (sin turno ahora o doble lectura).
    action: str | None = None
    message: str


class VerificationResult(Explained):
    """El resultado de una verificación o identificación; `message` se construye diferido (`Explained`)."""

    verified: bool
    method: VerificationMethod
    message: str
    employee_id: int | None = None
    employee_number: str | None = None
    name: str | None = None
    #: Foto de perfil de quien se verificó (ruta versionada) o None: la persona misma, su empresa o el validador de su
    #: empresa (los únicos que reciben este resultado) pueden verla.
    avatar: str | None = None
    confidence: float | None = None
    verified_at: datetime | None = None
    #: Solo en un validador: la entrada o salida del turno que registró esta identificación.
    attendance: ValidatorAttendance | None = None
    #: El motor de riesgo dejó el registro "en revisión": queda guardado, pero la empresa lo confirma o rechaza.
    review: bool = False
    #: Solo en un validador (antifraude 2b): el reto que firma su dispositivo en la siguiente identificación.
    device_nonce: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
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
    )


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


class CompanyVerificationRead(BaseModel):
    """Una verificación de identidad de la empresa con DÓNDE se hizo (pantalla «Verificaciones», mapa; decisión del
    dueño, 2026-10-07). Trae a la persona con su foto de perfil (`avatar`; la empresa ve a su gente) y, si la
    verificación llevó ubicación, su punto. Nunca fotos del registro facial.
    La empresa sale de la sesión (aislamiento)."""

    id: int
    created_at: datetime
    method: VerificationMethod
    success: bool
    #: Motivo del rechazo (catalog.verification_reasons); None si fue exitosa.
    reason: str | None
    #: Confianza del reconocimiento facial (None en el QR, que no la tiene).
    confidence: float | None
    #: Empleado (None cuando no se identificó a nadie, p. ej. un 1:N sin coincidencia).
    employee_id: int | None
    employee_number: str | None
    employee_name: str | None
    #: Ruta versionada de la foto de perfil dentro de la API (None sin foto o sin empleado).
    avatar: str | None = None
    #: Dónde se hizo (WGS-84); None cuando la verificación no llevó ubicación.
    latitude: float | None
    longitude: float | None
    location_accuracy_m: int | None


class CompanyVerificationList(Page[CompanyVerificationRead]):
    """Página de las verificaciones de la empresa (la más reciente primero), para el mapa de «Verificaciones»."""
