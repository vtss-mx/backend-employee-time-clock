from datetime import datetime

from sqlalchemy import DateTime, Double, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_schemas import CATALOG


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AddressMixin:
    """Domicilio con su punto en el mapa (WGS84): las mismas columnas que el esquema `Address`.

    Lo comparten los registros con lugar físico (validadores, sitios de trabajo): una sola definición
    para las columnas, la validación (`app/schemas/address.py`) y su lectura y escritura."""

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
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)
