from datetime import datetime

from pydantic import BaseModel, Field


class FaceCheckResponse(BaseModel):
    """Resultado de la validación previa de una imagen (sin comparar identidad)."""

    ok: bool = True
    message: str = "Imagen válida"
    detection_score: float
    quality_score: float
    yaw_ratio: float | None = None


class FaceLearningSummary(BaseModel):
    """Evolución del reconocimiento facial de la empresa: lo que su galería aprendió del uso."""

    enabled: bool = Field(description="La empresa tiene activo el aprendizaje continuo (política)")
    approved_employees: int = Field(description="Empleados activos con rostro aprobado")
    employees_learning: int = Field(description="Empleados con al menos una muestra aprendida")
    learned_samples: int = Field(description="Muestras aprendidas vigentes")
    identifications: int = Field(description="Identificaciones exitosas decididas por las muestras vigentes")
    learned_identifications: int = Field(description="De ellas, las que decidió una muestra aprendida")
    last_learned_at: datetime | None = Field(default=None, description="Cuándo se aprendió la muestra más reciente")
