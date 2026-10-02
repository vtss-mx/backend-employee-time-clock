from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import EnrollmentStatus, FaceStatus


class EnrollmentSubmitResponse(BaseModel):
    enrollment_id: int
    face_status: FaceStatus
    message: str = "Tu registro facial fue enviado y está en validación"


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
    rejection_reason: str | None = None


class FaceEnrollmentDetail(FaceEnrollmentRead):
    #: Fotografía de referencia (data URL) para validar la identidad. None si fue eliminada.
    photo: str | None = None


class FaceEnrollmentList(BaseModel):
    items: list[FaceEnrollmentRead]
    total: int
    page: int
    size: int


class EnrollmentRejectRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500, description="Motivo que verá el empleado")

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("Indica el motivo del rechazo")
        return value
