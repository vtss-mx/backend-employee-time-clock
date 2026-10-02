from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.orm import Session

from app.models import AuthSession


class SessionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, session_id: str, *, for_update: bool = False) -> AuthSession | None:
        # Se bloquea solo la fila de la sesión (no la del usuario unido por el eager load).
        lock = {"of": AuthSession} if for_update else None
        return self.db.get(AuthSession, session_id, with_for_update=lock)

    def add(self, session: AuthSession) -> None:
        self.db.add(session)

    def active_for_user(self, user_id: int, now: datetime) -> list[AuthSession]:
        return list(
            self.db.scalars(
                select(AuthSession)
                .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None), AuthSession.expires_at > now)
                .order_by(AuthSession.created_at.desc())
            )
        )

    def revoke_all(
        self, user_id: int, now: datetime, reason: str, *, except_id: str | None = None, company_id: int | None = None
    ) -> int:
        """Sesiones activas del usuario (todas, o solo las que entraron a `company_id`)."""
        stmt = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        if except_id:
            stmt = stmt.where(AuthSession.id != except_id)
        if company_id is not None:
            stmt = stmt.where(AuthSession.company_id == company_id)
        result = cast(
            CursorResult[Any],
            self.db.execute(
                stmt.values(revoked_at=now, revoked_reason=reason).execution_options(synchronize_session="fetch")
            ),
        )
        return result.rowcount or 0

    def purge_expired(self, before: datetime) -> None:
        self.db.execute(
            delete(AuthSession).where(AuthSession.expires_at < before).execution_options(synchronize_session=False)
        )
