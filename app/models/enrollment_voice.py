from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import BIOMETRICS, CATALOG
from app.models.mixins import company_fk


class EnrollmentVoiceAnswer(Base):
    """Una respuesta ACEPTADA de la verificación por voz y video de un registro facial (decisión del dueño del
    producto, 2026-10-06).

    Tras las fotos válidas, el empleado responde en video tres preguntas sobre sus propios datos; cada respuesta que
    pasa (se oyó, coincide con el dato registrado y el rostro del video es el de las fotos) deja aquí su fila con la
    pregunta, cuántos intentos tomó, lo que se oyó y la REFERENCIA de su clip, que vive CIFRADO en el bucket
    (`STORED_IMAGES`, `ENROLLMENT_VOICE_CLIPS`), nunca en la base. Lo revisa la empresa al validar el registro; el
    ADMIN de la plataforma no lo ve (dato biométrico: la voz y el rostro de una persona). Se borra DE VERDAD con el
    empleado (`person_erasure`) y, si no, a los `FACE_VIDEO_RETENTION_DAYS` días (depuración por lotes, su objeto a
    la cola del bucket en la misma sentencia).
    """

    __tablename__ = "enrollment_voice_answers"
    __table_args__ = (
        # Una respuesta aceptada por pregunta de la sesión; es también el índice del detalle de la revisión
        # (WHERE company_id, enrollment_id ORDER BY position) y de lo que se libera al rechazar un registro.
        UniqueConstraint("company_id", "enrollment_id", "position", name="uq_enrollment_voice_answers_position"),
        # Lo que se borra de verdad al eliminar al empleado (`erase_employee`) y sale del bucket con él.
        Index("ix_enrollment_voice_answers_employee", "company_id", "employee_id"),
        # Retención: los clips vencen a los FACE_VIDEO_RETENTION_DAYS (la depuración los busca por aquí).
        Index("ix_enrollment_voice_answers_created", "created_at"),
        # El registro facial al que responde es de la MISMA empresa (y se va con él).
        company_fk("enrollment_voice_answers", "enrollment_id", f"{BIOMETRICS}.face_enrollments"),
        CheckConstraint("position >= 0 AND attempts >= 1", name="position_attempts"),
        {"schema": BIOMETRICS},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    enrollment_id: Mapped[int] = mapped_column(nullable=False)
    #: El empleado del registro (copia: su borrado real no necesita un JOIN).
    employee_id: Mapped[int] = mapped_column(nullable=False)
    #: Qué se preguntó (catalog.voice_questions) y en qué lugar de la sesión (0 = la primera).
    question: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.voice_questions.code"), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Intentos que tomó esta pregunta (1 = a la primera) y lo que se oyó en el que pasó (lo ve la empresa).
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    transcript: Mapped[str | None] = mapped_column(String(200))
    #: Parecido de la respuesta con el dato (0-1) y del rostro del video con las fotos del registro (0-1).
    similarity: Mapped[float | None] = mapped_column(Float)
    face_similarity: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Parte aleatoria del nombre del objeto (se sube ANTES de insertar la fila: ninguna conexión espera al bucket).
    uid: Mapped[str] = mapped_column(String(32), nullable=False)
    #: La referencia del clip cifrado en el bucket (`image_storage`): tipo, objeto, tamaño, SHA-256 y cuándo se subió.
    content_type: Mapped[str | None] = mapped_column(String(30))
    object_name: Mapped[str | None] = mapped_column(String(300))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
