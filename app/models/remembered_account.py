from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import AUTH

if TYPE_CHECKING:
    from app.models.user import User


class RememberedAccount(Base):
    """ "Recordar mi cuenta" en un dispositivo: el login muestra el correo ya escrito.

    El dispositivo solo guarda una cookie HttpOnly opaca (`<id>.<secreto>`); el dato (qué cuenta
    recordar) vive aquí, con el secreto como hash SHA-256. Sobrevive al cierre de sesión y se
    borra al desmarcar la casilla, al pulsar "Usar otra cuenta" o al vencer.
    """

    __tablename__ = "remembered_accounts"
    __table_args__ = ({"schema": AUTH},)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="CASCADE"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)

    # joined: leer el correo recordado es una sola consulta.
    user: Mapped[User] = relationship(lazy="joined", innerjoin=True)
