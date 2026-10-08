from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import as_utc
from app.core.database import Base
from app.core.db_schemas import BIOMETRICS, WORKFORCE
from app.models.mixins import company_fk


class FaceEnrollmentDraft(Base):
    """El paso 1 del registro facial del propio empleado: su foto inicial aceptada (decisión del dueño del producto,
    2026-10-07: los tres pasos del registro —foto inicial, capturas con prueba de vida y video con preguntas— son
    independientes y retomables otro día; el servidor exige su orden).

    Antes la foto inicial solo se validaba (`POST /face/check`) y no quedaba nada: el paso 2 seguía en la misma
    pantalla. Ahora `POST /enrollment/photo` la guarda aquí como BORRADOR del registro: la foto CIFRADA en el bucket
    (`STORED_IMAGES`, `FACE_ENROLLMENT_DRAFT_PHOTOS`; aquí solo su referencia, regla 13 de la raíz) y su plantilla
    facial cifrada (`template_encrypted`: un vector de números, como `face_embeddings.embedding_encrypted`; nunca una
    imagen), con que el paso 2 comprueba que las capturas son de la MISMA persona que la foto inicial
    (`FACE_ENROLL_CONSISTENCY_THRESHOLD`). Un empleado tiene a lo más un borrador (único por empresa y empleado):
    «Repetir foto» lo reemplaza y el objeto anterior pasa a la cola del bucket. Vence a las
    `FACE_ENROLLMENT_DRAFT_HOURS` (`expires_at`: vencido, el paso 2 responde 409 y la depuración lo borra con su
    objeto); al aceptar las capturas, su foto PASA A SER la foto de referencia del registro (la que revisa la empresa:
    el objeto cifrado cambia de dueño, no se vuelve a subir) y la fila sale. Se borra DE VERDAD con la persona
    (`person_erasure`).

    Tabla de empresa (seguridad por fila, `TENANT_TABLES`) con FK compuesta al empleado: la base rechaza un borrador de
    otra empresa. Es tan pequeña como corta su vida (horas): no entra en la foto diaria del almacenamiento.
    """

    __tablename__ = "face_enrollment_drafts"
    __table_args__ = (
        # Un borrador por empleado; también es el índice de «el borrador de este empleado» (WHERE company_id,
        # employee_id) y el de la FK compuesta (sus dos columnas en igualdad).
        UniqueConstraint("company_id", "employee_id", name="uq_face_enrollment_drafts_employee"),
        # La depuración de lo vencido (`PURGES`: WHERE expires_at <= now, por lotes) sin recorrer la tabla.
        Index("ix_face_enrollment_drafts_expires", "expires_at"),
        # El empleado es de la MISMA empresa (FK compuesta); se va con él en cascada.
        company_fk("face_enrollment_drafts", "employee_id", f"{WORKFORCE}.employees"),
        # Ids que nunca se reutilizan (como en PostgreSQL) también en SQLite (pruebas): el objeto de la foto se nombra
        # con el id y un borrador reemplazado deja su objeto en la cola del bucket un rato más.
        {"schema": BIOMETRICS, "sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    employee_id: Mapped[int] = mapped_column(nullable=False)
    #: La plantilla facial de la foto inicial (cifrada con `DATA_ENCRYPTION_KEY`) con el modelo que la calculó y su
    #: dimensión: el paso 2 la compara con las referencias elegidas entre las capturas.
    template_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    model_name: Mapped[str] = mapped_column(String(50), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    detection_score: Mapped[float] = mapped_column(Float, nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    #: La foto vive CIFRADA en el bucket (`image_storage`); aquí solo su referencia: nombre del objeto, tamaño de la
    #: imagen, SHA-256 del objeto cifrado y cuándo se subió (verificada).
    photo_content_type: Mapped[str | None] = mapped_column(String(30))
    photo_object: Mapped[str | None] = mapped_column(String(300))
    photo_size: Mapped[int | None] = mapped_column(Integer)
    photo_sha256: Mapped[str | None] = mapped_column(String(64))
    photo_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Cuándo se aceptó la foto y hasta cuándo sirve para el paso 2 (`FACE_ENROLLMENT_DRAFT_HOURS`).
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def active_at(self, moment: datetime) -> bool:
        """¿Sigue sirviendo para el paso 2 en este instante? (SQLite devuelve la fecha sin zona: se lee como UTC.)"""
        return as_utc(self.expires_at) > moment
