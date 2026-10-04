from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ColumnElement,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    false,
    func,
    text,
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

    La galería de cada empleado evoluciona con el uso (`app/services/face_learning.py`): las muestras
    del registro aprobado (`learned = False`) son el ancla y nunca se reemplazan; las aprendidas de
    identificaciones seguras compiten por su lugar según su utilidad (`matches`, `last_matched_at`).
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
        CheckConstraint("matches >= 0", name="matches_non_negative"),
        # Ids que nunca se reutilizan (como en PostgreSQL) también en SQLite (pruebas): la huella de la
        # galería en memoria usa el id mayor para notar que una muestra aprendida reemplazó a otra.
        {"schema": BIOMETRICS, "sqlite_autoincrement": True},
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
    #: Aprendida de una identificación segura (no la aprobó una persona): puede dejar su lugar.
    learned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: Veces que fue la muestra más parecida en una identificación exitosa (su utilidad).
    matches: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    last_matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    employee: Mapped[Employee] = relationship(back_populates="face_embeddings", foreign_keys=[employee_id])


def last_useful() -> ColumnElement[datetime]:
    """Último momento en que la muestra sirvió: la última vez que decidió una identificación o, si
    nunca lo hizo, cuando se aprendió."""
    return func.coalesce(FaceEmbedding.last_matched_at, FaceEmbedding.created_at)


#: El mantenimiento retira las muestras aprendidas que dejaron de servir sin recorrer la tabla.
Index(
    "ix_face_embeddings_learned_last_useful",
    last_useful().label("last_useful"),
    postgresql_where=text("learned"),
    sqlite_where=text("learned"),
)
