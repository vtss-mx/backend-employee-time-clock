from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.i18n import LocalizedValueError, StoredText, t
from app.models.enums import EnrollmentStatus, FaceStatus
from app.schemas.common import Page


class EnrollmentSubmitResponse(BaseModel):
    enrollment_id: int
    face_status: FaceStatus
    #: Para la persona, en el idioma de la petición (se arma al crear la respuesta).
    message: str = Field(default_factory=lambda: t("ENROLLMENT_SENT"))


class FaceEnrollmentRead(BaseModel):
    id: int
    status: EnrollmentStatus
    employee_id: int
    employee_number: str
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


class SimilarEmployee(BaseModel):
    """Un empleado aprobado cuyo rostro se parece al del registro (marca POSSIBLE_DUPLICATE)."""

    employee_id: int
    full_name: str
    employee_number: str
    #: Similitud (0-1) de las capturas del registro con su rostro.
    similarity: float


class FaceEnrollmentDetail(FaceEnrollmentRead):
    #: Fotografía de referencia (data URL) para validar la identidad. None si fue eliminada.
    photo: str | None = None
    #: Los empleados más parecidos (nivel de sospecha de la empresa): revísalos antes de aprobar.
    similar: list[SimilarEmployee] = []


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
