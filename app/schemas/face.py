from datetime import datetime

from pydantic import BaseModel


class FaceRegistrationResponse(BaseModel):
    employee_id: int
    face_samples: int
    samples_added: int
    detection_score: float
    quality_score: float
    model_name: str
    registered_at: datetime
    message: str = "Rostro registrado correctamente"


class FaceCheckResponse(BaseModel):
    """Resultado de la validación previa de una imagen (sin comparar identidad)."""

    ok: bool = True
    message: str = "Imagen válida"
    detection_score: float
    quality_score: float
    yaw_ratio: float | None = None
