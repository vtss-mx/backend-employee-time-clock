from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import WORKFORCE
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.employee import Employee


class EmployeeQr(TimestampMixin, Base):
    """Código QR de identificación de un empleado.

    - `token_hash`: SHA-256 del token aleatorio; se usa para buscar el QR al verificar.
    - `token_encrypted`: token cifrado (Fernet) solo para que COMPANY pueda volver a
      mostrar/descargar la imagen. Nunca se guarda el token en texto plano.
    - Solo un QR activo por empleado; regenerar desactiva el anterior.
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
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey(f"{WORKFORCE}.employees.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    employee: Mapped["Employee"] = relationship(back_populates="qr_codes")
