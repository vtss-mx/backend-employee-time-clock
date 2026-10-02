from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, ForeignKeyConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY, WORKFORCE
from app.models.enums import ValidatorMode
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.user import User


class Validator(TimestampMixin, Base):
    """Validador de identidad de una empresa (p. ej. "Recepción planta 1").

    Su cuenta (auth.users, rol VALIDATOR) inicia sesión en una tableta o un teléfono e identifica
    a los empleados de SU empresa según su modo: QR, rostro, cualquiera o ambos.
    """

    __tablename__ = "validators"
    __table_args__ = (
        # Listado de la empresa en orden alfabético.
        Index("ix_validators_company_name", "company_id", "name"),
        # La cuenta del validador es de la MISMA empresa que el validador (lo garantiza la base).
        ForeignKeyConstraint(
            ["user_id", "company_id"],
            [f"{AUTH}.users.id", f"{AUTH}.users.company_id"],
            name="fk_validators_user_company",
            ondelete="CASCADE",
        ),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey(f"{AUTH}.users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[ValidatorMode] = mapped_column(
        Enum(ValidatorMode, native_enum=False, length=20, validate_strings=True),
        ForeignKey(f"{CATALOG}.validator_modes.code"),
        default=ValidatorMode.QR_OR_FACE,
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="validator", foreign_keys=[user_id], lazy="joined")
    company: Mapped["Company"] = relationship(lazy="joined")
