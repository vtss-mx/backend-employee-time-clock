from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import AUTH, WORKFORCE
from app.models.mixins import TimestampMixin, company_fk

if TYPE_CHECKING:
    from app.models.employee import Employee


class EmployeeQr(TimestampMixin, Base):
    """Código QR DINÁMICO de un empleado: vive unos segundos y sirve UNA sola vez.

    El empleado lo muestra en su teléfono; se renueva solo (cada `qr_lifetime_seconds` de la
    política de su empresa) o cuando lo pide, y al usarse queda consumido para siempre.

    - `token_hash`: SHA-256 del token aleatorio; el token nunca se guarda (ni cifrado).
    - `active`: el QR vigente del empleado (uno a la vez; emitir otro o usarlo lo apaga).
    - `used_at` / `used_by_id`: cuándo y quién lo escaneó (el validador). Se marca en una sola
      sentencia atómica: dos lecturas simultáneas del mismo QR no pueden ganar ambas.
    - `completed_at`: el uso terminó. Con QR basta el escaneo; con QR + rostro, el escaneo aparta el
      QR para ese validador y se completa al comparar el rostro (o vence a QR_FACE_WINDOW_SECONDS).
    """

    __tablename__ = "employee_qr_codes"
    __table_args__ = (
        # Un solo QR activo por empleado, garantizado por la BD (índice único parcial); también
        # resuelve en una lectura "el QR vigente de este empleado".
        Index(
            "uq_employee_qr_codes_active_employee",
            "employee_id",
            unique=True,
            postgresql_where=text("active IS TRUE"),
            sqlite_where=text("active = 1"),
        ),
        # FK con ON DELETE SET NULL (quién lo usó): evita recorrer la tabla al borrar un usuario.
        Index(
            "ix_employee_qr_codes_used_by_id",
            "used_by_id",
            postgresql_where=text("used_by_id IS NOT NULL"),
            sqlite_where=text("used_by_id IS NOT NULL"),
        ),
        # Último QR generado de un empleado (WHERE employee_id ORDER BY id DESC LIMIT 1).
        Index("ix_employee_qr_codes_employee_latest", "employee_id", "id"),
        # Último uso de un empleado (WHERE employee_id AND used_at IS NOT NULL ORDER BY used_at DESC).
        Index(
            "ix_employee_qr_codes_employee_used",
            "employee_id",
            "used_at",
            "id",
            postgresql_where=text("used_at IS NOT NULL"),
            sqlite_where=text("used_at IS NOT NULL"),
        ),
        # El empleado es de la MISMA empresa del QR (FK compuesta; también la usa el borrado en cascada).
        company_fk("employee_qr_codes", "employee_id", f"{WORKFORCE}.employees"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Empresa del empleado (copia): la seguridad por fila aísla los QR por empresa.
    company_id: Mapped[int] = mapped_column(nullable=False)
    employee_id: Mapped[int] = mapped_column(nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Vencimiento (se depuran los vencidos tras QR_TOKEN_RETENTION_DAYS).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    employee: Mapped[Employee] = relationship(
        back_populates="qr_codes", primaryjoin="EmployeeQr.employee_id == Employee.id", foreign_keys=[employee_id]
    )
