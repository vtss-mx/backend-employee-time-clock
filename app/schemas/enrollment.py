import base64
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.i18n import LocalizedValueError, StoredText, t
from app.models.enums import EnrollmentStatus, FaceStatus, VoiceQuestion
from app.schemas.common import Page
from app.schemas.face import FaceCheckResponse


class VoiceQuestionRead(BaseModel):
    """Una pregunta de la sesión: su lugar, su código (catalog.voice_questions) y su TEXTO ya renderizado en el idioma
    de la petición (decisión del dueño, 2026-10-07): la app y los SDK solo lo muestran. La suma llega con sus números
    sustituidos («¿Cuánto es 7 más 4?»); en las de identidad, el catálogo da el mismo texto por su código."""

    position: int
    question: VoiceQuestion
    text: str


class VoiceChallengeResponse(BaseModel):
    """La verificación por voz y video que sigue a las fotos (decisión del dueño, 2026-10-06): el token sellado de la
    sesión (viaja con cada respuesta y vuelve renovado), las preguntas que FALTAN en orden (decisión del dueño,
    2026-10-07: una sesión que se retoma otro día conserva las respuestas aceptadas y sigue con la pregunta que falta;
    `answered` de `total` ya pasaron), cuánto debe durar cada respuesta, los intentos por pregunta y la vida de la
    sesión."""

    token: str
    questions: list[VoiceQuestionRead]
    #: Preguntas de la sesión completa y cuántas ya tienen su respuesta aceptada (la app muestra «2 de 3 respondidas»).
    total: int
    answered: int
    min_seconds: float
    max_seconds: float
    retries: int
    expires_in: int


class EnrollmentSubmitResponse(BaseModel):
    enrollment_id: int
    face_status: FaceStatus
    #: Para la persona, en el idioma de la petición (se arma al crear la respuesta).
    message: str = Field(default_factory=lambda: t("ENROLLMENT_SENT"))
    #: Con la verificación por voz de la política: las preguntas que siguen (el registro aún no termina). Se conserva
    #: por compatibilidad con la app anterior (un solo flujo); la app actual pide su sesión con
    #: `POST /enrollment/voice/start` cuando la persona abre el paso 3.
    voice: VoiceChallengeResponse | None = None


class EnrollmentPhotoResponse(FaceCheckResponse):
    """La foto inicial aceptada y guardada como borrador del registro (paso 1 de 3; decisión del dueño, 2026-10-07): lo
    mismo que la validación previa (calidad, pose, accesorios para las insignias) más cuándo se aceptó y hasta cuándo
    sirve para el paso 2 (`FACE_ENROLLMENT_DRAFT_HOURS`)."""

    checked_at: datetime
    expires_at: datetime


#: Paso 1: sin foto, con la foto vigente o con una vencida (hay que repetirla).
type PhotoStepStatus = Literal["pending", "done", "expired"]
#: Paso 2: bloqueado (falta la foto vigente), por hacer o hecho (las capturas aceptadas).
type CaptureStepStatus = Literal["locked", "pending", "done"]
#: Paso 3: la política no lo pide, bloqueado (faltan las capturas), por hacer (o a medias), con los intentos agotados
#: (se repiten los pasos 1 y 2) o hecho.
type VoiceStepStatus = Literal["not_required", "locked", "pending", "exhausted", "done"]


class PhotoStep(BaseModel):
    status: PhotoStepStatus
    #: Cuándo se aceptó la foto y hasta cuándo sirve (null cuando las capturas ya la consumieron: la fecha es la del
    #: envío).
    checked_at: datetime | None = None
    expires_at: datetime | None = None


class CaptureStep(BaseModel):
    status: CaptureStepStatus
    submitted_at: datetime | None = None


class VoiceStep(BaseModel):
    status: VoiceStepStatus
    #: Respuestas aceptadas de las preguntas de la sesión y los intentos fallidos que quedan (null si no aplica).
    answered: int = 0
    total: int = 0
    attempts_left: int | None = None


class EnrollmentProgress(BaseModel):
    """El estado de los tres pasos independientes del registro facial (decisión del dueño, 2026-10-07) para el índice
    de la app: se deriva del borrador (paso 1), del registro pendiente (paso 2) y de sus respuestas (paso 3)."""

    face_status: FaceStatus
    photo: PhotoStep
    capture: CaptureStep
    voice: VoiceStep


class VoiceAnswerResponse(BaseModel):
    """Una respuesta aceptada: el token renovado, qué pregunta fue, si ya pasaron todas y cuál sigue."""

    token: str
    position: int
    done: bool
    next_position: int | None = None


class VoiceAnswerRead(BaseModel):
    """Lo que la empresa ve de cada respuesta aceptada al revisar el registro."""

    id: int
    position: int
    question: VoiceQuestion
    #: Intentos que tomó la pregunta (1 = a la primera).
    attempts: int
    #: Lo que se oyó (las palabras de la persona: un dato, se muestra tal cual) y los parecidos medidos (0-1).
    transcript: str | None = None
    similarity: float | None = None
    face_similarity: float | None = None
    duration_ms: int
    created_at: datetime
    #: El clip sigue en el bucket (sale a los FACE_VIDEO_RETENTION_DAYS).
    has_clip: bool


class EnrollmentVoiceRead(BaseModel):
    """La verificación por voz de un registro: si pasó, cuántas respuestas no pasaron y las aceptadas."""

    required: bool
    passed_at: datetime | None = None
    failed_attempts: int = 0
    answers: list[VoiceAnswerRead] = []


class VoiceClipRead(BaseModel):
    """El video de una respuesta, descifrado y en base64 dentro del contrato (como un documento): nunca una URL del
    bucket."""

    content_type: str
    data: bytes
    byte_size: int
    duration_ms: int

    @field_serializer("data")
    def _base64(self, value: bytes) -> str:
        return base64.b64encode(value).decode()


class FaceEnrollmentRead(BaseModel):
    id: int
    status: EnrollmentStatus
    employee_id: int
    #: Opcional (migración 0076): null si el empleado no tiene número.
    employee_number: str | None = None
    full_name: str
    email: str
    birth_date: date
    employee_active: bool
    samples: int
    quality_score: float
    liveness_passed: bool
    #: Marcas para revisar en la foto antes de aceptar (catalog.enrollment_flags): accesorios que el
    #: sistema detectó y el empleado indicó no usar, o posible suplantación (SPOOF).
    flagged_accessories: list[str] = []
    submitted_at: datetime
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    #: Registro asistido: correo del administrador que capturó el rostro en persona.
    captured_by: str | None = None
    #: El que escribió la empresa o, si lo puso el sistema, en el idioma de quien lo lee (`app/i18n/stored.py`).
    rejection_reason: StoredText = None
    #: Foto de PERFIL del empleado (ruta versionada) o None: la bandeja la muestra junto al nombre. No es la foto del
    #: registro facial (`FaceEnrollmentDetail.photo`), que sigue sus propias reglas.
    avatar: str | None = None


class SimilarEmployee(BaseModel):
    """Un empleado aprobado cuyo rostro se parece al del registro (marca POSSIBLE_DUPLICATE)."""

    employee_id: int
    full_name: str
    #: Opcional (migración 0076): null si el empleado no tiene número.
    employee_number: str | None = None
    #: Similitud (0-1) de las capturas del registro con su rostro.
    similarity: float
    #: Su foto de perfil (ruta versionada) o None.
    avatar: str | None = None


class FaceEnrollmentDetail(FaceEnrollmentRead):
    #: Fotografía de referencia (data URL) para validar la identidad. None si fue eliminada.
    photo: str | None = None
    #: Los empleados más parecidos (nivel de sospecha de la empresa): revísalos antes de aprobar.
    similar: list[SimilarEmployee] = []
    #: La verificación por voz y video (decisión del dueño, 2026-10-06); None si el registro no la llevó.
    voice: EnrollmentVoiceRead | None = None


class FaceEnrollmentList(Page[FaceEnrollmentRead]):
    """Página de registros faciales (bandeja de validación o historial)."""


class EnrollmentRejectRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500, description="Motivo que verá el empleado")

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise LocalizedValueError("REJECTION_REASON_REQUIRED")
        return value
