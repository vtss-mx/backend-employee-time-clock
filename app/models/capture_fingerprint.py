from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import BIOMETRICS, TENANCY


class CaptureFingerprint(Base):
    """Huella de cada captura facial recibida (SHA-256 de sus píxeles, no la imagen).

    Una persona frente a la cámara nunca produce dos veces la misma imagen: si llega una captura
    con una huella ya vista, es un reenvío (replay) de una captura interceptada o guardada. Se
    conservan `FACE_REPLAY_RETENTION_DAYS` días.
    """

    __tablename__ = "capture_fingerprints"
    __table_args__ = (
        Index("ix_capture_fingerprints_company_id", "company_id"),
        # Limpieza de las vencidas (DELETE WHERE created_at < ...).
        Index("ix_capture_fingerprints_created_at", "created_at"),
        {"schema": BIOMETRICS},
    )

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
