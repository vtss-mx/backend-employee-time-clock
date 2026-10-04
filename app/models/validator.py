from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Double,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY, WORKFORCE
from app.models.enums import ValidatorMode
from app.models.mixins import TimestampMixin

#: Radio permitido para iniciar sesión (m): de una sala a un predio grande.
LOCATION_RADIUS_MIN_M = 10
LOCATION_RADIUS_MAX_M = 10_000

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.user import User


class Validator(TimestampMixin, Base):
    """Validador de identidad de una empresa (p. ej. "Recepción planta 1").

    Su cuenta (auth.users, rol VALIDATOR) inicia sesión en una tableta o un teléfono e identifica
    a los empleados de SU empresa según su modo: QR, rostro, cualquiera o ambos.

    Tiene el domicilio del acceso donde opera y su punto en el mapa. Si `location_required`, solo
    puede iniciar sesión a no más de `location_radius_m` metros de ese punto.
    """

    __tablename__ = "validators"
    __table_args__ = (
        # Listado de la empresa en orden alfabético sin distinguir mayúsculas (ORDER BY lower(name), id).
        Index("ix_validators_company_name", "company_id", text("lower(name)"), "id"),
        Index("ix_validators_country_code", "country_code"),
        # Latitud y longitud van juntas y dentro de sus rangos (WGS84).
        CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL) AND "
            "(latitude IS NULL OR (latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180))",
            name="coordinates",
        ),
        CheckConstraint(
            "location_radius_m IS NULL OR "
            f"location_radius_m BETWEEN {LOCATION_RADIUS_MIN_M} AND {LOCATION_RADIUS_MAX_M}",
            name="location_radius",
        ),
        # Exigir ubicación requiere el punto en el mapa y el radio.
        CheckConstraint(
            "NOT location_required OR (latitude IS NOT NULL AND location_radius_m IS NOT NULL)",
            name="location_point",
        ),
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

    # --- Domicilio del acceso donde opera ---
    street: Mapped[str | None] = mapped_column(String(150))
    exterior_number: Mapped[str | None] = mapped_column(String(20))
    interior_number: Mapped[str | None] = mapped_column(String(20))
    postal_code: Mapped[str | None] = mapped_column(String(10))
    country_code: Mapped[str | None] = mapped_column(
        String(2), ForeignKey(f"{CATALOG}.countries.code", ondelete="RESTRICT")
    )
    state: Mapped[str | None] = mapped_column(String(100))
    municipality: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(100))
    # --- Punto en el mapa (WGS84) y ubicación exigida al iniciar sesión ---
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)
    location_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    location_radius_m: Mapped[int | None] = mapped_column(Integer)

    user: Mapped[User] = relationship(back_populates="validator", foreign_keys=[user_id], lazy="joined")
    company: Mapped[Company] = relationship(lazy="joined")
