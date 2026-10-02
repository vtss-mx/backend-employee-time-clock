from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, func, text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY


class VerificationPolicy(Base):
    """Política de verificación de cada empresa (una fila por empresa), editable por COMPANY.

    Define qué se exige en cada captura facial y qué métodos de identificación se permiten.
    """

    __tablename__ = "verification_policy"
    __table_args__ = (
        # FK con ON DELETE SET NULL (quién la cambió): evita recorrer la tabla al borrar un usuario.
        Index(
            "ix_verification_policy_updated_by_id",
            "updated_by_id",
            postgresql_where=text("updated_by_id IS NOT NULL"),
            sqlite_where=text("updated_by_id IS NOT NULL"),
        ),
        {"schema": TENANCY},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    block_glasses: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    block_headwear: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    block_mask: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    liveness_challenge: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    anti_spoofing: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    qr_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Confianza mínima (probabilidad de que sea la misma persona) para aceptar una verificación facial:
    # uno de los niveles del catálogo (catalog.confidence_levels.value).
    min_confidence: Mapped[float] = mapped_column(
        Numeric(7, 5, asdecimal=False),
        ForeignKey(f"{CATALOG}.confidence_levels.value"),
        default=0.99999,
        server_default=text("0.99999"),
        nullable=False,
    )
    # Los empleados solo pueden usar la aplicación desde un teléfono celular.
    employee_mobile_only: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    # Los validadores de identidad solo operan desde una tableta o un teléfono (no computadoras).
    validator_mobile_only: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
