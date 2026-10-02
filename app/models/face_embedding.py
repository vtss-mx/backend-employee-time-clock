from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import BIOMETRICS, WORKFORCE

if TYPE_CHECKING:
    from app.models.employee import Employee


class FaceEmbedding(Base):
    """Vector de características facial de un empleado.

    Solo se guarda el embedding (cifrado con Fernet); nunca la fotografía.
    `model_name` permite migrar a otro modelo sin mezclar vectores incompatibles.
    """

    __tablename__ = "face_embeddings"
    __table_args__ = (
        # Cada comparación facial lee los embeddings ACTIVOS de un empleado en orden de captura.
        Index("ix_face_embeddings_employee_active", "employee_id", "active", "created_at"),
        # El embedding es del mismo empleado que su registro facial (lo garantiza la base).
        ForeignKeyConstraint(
            ["enrollment_id", "employee_id"],
            [f"{BIOMETRICS}.face_enrollments.id", f"{BIOMETRICS}.face_enrollments.employee_id"],
            name="fk_face_embeddings_enrollment_employee",
            ondelete="CASCADE",
        ),
        CheckConstraint("dimension > 0", name="dimension_positive"),
        {"schema": BIOMETRICS},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey(f"{WORKFORCE}.employees.id", ondelete="CASCADE"), nullable=False
    )
    # Registro facial del que proviene; los embeddings solo se activan al aprobarse.
    enrollment_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{BIOMETRICS}.face_enrollments.id", ondelete="CASCADE"), index=True
    )
    embedding_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    model_name: Mapped[str] = mapped_column(String(50), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    detection_score: Mapped[float] = mapped_column(Float, nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    employee: Mapped["Employee"] = relationship(back_populates="face_embeddings", foreign_keys=[employee_id])
