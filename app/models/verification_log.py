from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, ForeignKeyConstraint, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import ATTENDANCE, AUTH, CATALOG, TENANCY, WORKFORCE
from app.core.partitions import partitioned
from app.models.enums import VerificationMethod


class VerificationLog(Base):
    """Registro (auditoría) de cada intento de identificación, exitoso o no.

    Crece sin límite: particionada por mes en `created_at` (`app/core/partitions.py`; en PostgreSQL la llave
    primaria es `(id, created_at)`). Siempre de UNA empresa (`company_id NOT NULL`) y su empleado es de esa
    misma empresa (FK compuesta): la base no acepta un intento que mezcle empresas.
    """

    __tablename__ = "verification_logs"
    __table_args__ = (
        # Historial de un empleado: WHERE employee_id ORDER BY created_at DESC, id DESC LIMIT n.
        Index("ix_verification_logs_employee_created", "employee_id", "created_at", "id"),
        # Bitácora de una empresa en orden de llegada: la API de integración la recorre por cursor (id).
        Index("ix_verification_logs_company_log", "company_id", "id"),
        # Súper-índice del listado de la empresa por periodo (migración 0047): `since`/`until` en el orden
        # pedido (la más reciente primero) y el conteo con tope (filtros de empleado y resultado) sin
        # leer la tabla.
        Index(
            "ix_verification_logs_company_created",
            "company_id",
            "created_at",
            "id",
            postgresql_include=["employee_id", "success"],
        ),
        # Quién hizo el intento (el empleado o el validador): historial reciente de un validador
        # (WHERE user_id ORDER BY created_at DESC, id DESC), sus identificaciones del día y el
        # bloqueo por fallos seguidos. INCLUDE: esos conteos se resuelven sin leer la tabla.
        Index(
            "ix_verification_logs_user_created",
            "user_id",
            "created_at",
            "id",
            # company_id: con la seguridad por fila el conteo sigue sin leer la tabla (la política lo pide).
            postgresql_include=["success", "method", "company_id"],
            postgresql_where=text("user_id IS NOT NULL"),
            sqlite_where=text("user_id IS NOT NULL"),
        ),
        # El empleado (si se identificó a alguien) es de la MISMA empresa del intento.
        ForeignKeyConstraint(
            ["employee_id", "company_id"],
            [f"{WORKFORCE}.employees.id", f"{WORKFORCE}.employees.company_id"],
            name="fk_verification_logs_employee_company",
            ondelete="CASCADE",
        ),
        {"schema": ATTENDANCE, **partitioned("created_at")},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int | None] = mapped_column()
    user_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    method: Mapped[VerificationMethod] = mapped_column(
        Enum(VerificationMethod, native_enum=False, length=10, validate_strings=True),
        ForeignKey(f"{CATALOG}.verification_methods.code"),
        nullable=False,
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    #: Motivo del rechazo (catalog.verification_reasons); None si fue exitoso.
    reason: Mapped[str | None] = mapped_column(String(50), ForeignKey(f"{CATALOG}.verification_reasons.code"))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
