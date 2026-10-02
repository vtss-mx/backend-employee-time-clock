from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY

if TYPE_CHECKING:
    from app.models.user import User


class AuthSession(Base):
    """Sesión de inicio de sesión (un dispositivo/navegador).

    El access token JWT lleva `sid`: revocar la sesión invalida de inmediato sus tokens.
    El refresh token (opaco, en cookie HttpOnly) se guarda solo como hash SHA-256 y rota en
    cada uso; presentar uno ya rotado fuera de la ventana de gracia revoca la sesión (robo).
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (
        # Sesiones de un usuario, de la más reciente a la más antigua (también sirve a la FK).
        Index("ix_auth_sessions_user_created", "user_id", "created_at"),
        {"schema": AUTH},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="CASCADE"), nullable=False)
    refresh_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_refresh_hash: Mapped[str | None] = mapped_column(String(64))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Motivo del cierre (catalog.session_revocation_reasons): decide qué se le dice a la persona.
    revoked_reason: Mapped[str | None] = mapped_column(
        String(40), ForeignKey(f"{CATALOG}.session_revocation_reasons.code")
    )
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    #: "Recordar mi cuenta": la cookie sobrevive al cierre del navegador (hasta que vence la
    #: sesión). Sin marcar, es una cookie de sesión del navegador.
    persistent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    #: Empresa en la que entró el empleado (la elige al iniciar sesión si trabaja en varias).
    #: Vive en la sesión del servidor: el cliente nunca la envía en cada petición.
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), index=True
    )

    # joined: validar la sesión y cargar el usuario es UNA sola consulta por petición.
    user: Mapped["User"] = relationship(lazy="joined", innerjoin=True)


class RateLimitCounter(Base):
    """Contador de ventana fija compartido por todos los procesos (RATE_LIMIT_BACKEND=database)."""

    __tablename__ = "rate_limit_counters"
    __table_args__ = ({"schema": AUTH},)

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    count: Mapped[int] = mapped_column(nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
