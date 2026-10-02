from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Index, LargeBinary, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import AUTH, BIOMETRICS, CATALOG, TENANCY, WORKFORCE
from app.models.enums import EnrollmentStatus

if TYPE_CHECKING:
    from app.models.employee import Employee


class FaceEnrollment(Base):
    """Solicitud de registro facial hecha por el empleado y revisada por COMPANY.

    `photo_encrypted` es una fotografía de referencia (cifrada con Fernet) para que el
    administrador valide la identidad. Se elimina si el registro es rechazado.
    """

    __tablename__ = "face_enrollments"
    __table_args__ = (
        # Último registro de un empleado: WHERE employee_id ORDER BY submitted_at DESC, id DESC LIMIT 1.
        Index("ix_face_enrollments_employee_submitted", "employee_id", "submitted_at", "id"),
        # Bandeja de validaciones de una empresa: WHERE company_id, status ORDER BY submitted_at, id.
        Index("ix_face_enrollments_company_status", "company_id", "status", "submitted_at", "id"),
        # FK con ON DELETE SET NULL (revisor): evita recorrer la tabla al borrar un usuario.
        Index(
            "ix_face_enrollments_reviewed_by_id",
            "reviewed_by_id",
            postgresql_where=text("reviewed_by_id IS NOT NULL"),
            sqlite_where=text("reviewed_by_id IS NOT NULL"),
        ),
        {"schema": BIOMETRICS},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey(f"{WORKFORCE}.employees.id", ondelete="CASCADE"), nullable=False
    )
    # Copia de la empresa del empleado: la bandeja se filtra por empresa sin unir tablas.
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[EnrollmentStatus] = mapped_column(
        Enum(EnrollmentStatus, native_enum=False, length=20, validate_strings=True),
        ForeignKey(f"{CATALOG}.enrollment_statuses.code"),
        nullable=False,
    )
    photo_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    photo_content_type: Mapped[str | None] = mapped_column(String(30))
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    samples: Mapped[int] = mapped_column(nullable=False)
    liveness_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    rejection_reason: Mapped[str | None] = mapped_column(String(500))

    employee: Mapped["Employee"] = relationship(lazy="joined")
    #: Marcas para el revisor: accesorios que el sistema detectó y el empleado indicó no usar, o
    #: posible suplantación. El administrador lo confirma al revisar la fotografía.
    flags: Mapped[list["FaceEnrollmentFlag"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", passive_deletes=True, order_by="FaceEnrollmentFlag.flag_code"
    )

    @property
    def flag_codes(self) -> list[str]:
        return [flag.flag_code for flag in self.flags]


class FaceEnrollmentFlag(Base):
    """Marca de un registro facial para su revisión (catalog.enrollment_flags)."""

    __tablename__ = "face_enrollment_flags"
    __table_args__ = ({"schema": BIOMETRICS},)

    enrollment_id: Mapped[int] = mapped_column(
        ForeignKey(f"{BIOMETRICS}.face_enrollments.id", ondelete="CASCADE"), primary_key=True
    )
    flag_code: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.enrollment_flags.code"), primary_key=True)
