"""Sesiones de usuario: emisión, rotación de refresh tokens y revocación.

Refresh token = "<sid>.<secreto aleatorio de 256 bits>". Solo se guarda el SHA-256 del
secreto. En cada /auth/refresh el secreto rota; si llega uno ya rotado (fuera de la ventana
de gracia para pestañas concurrentes) se asume robo y se revoca la sesión completa.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.crypto import hash_token
from app.core.exceptions import AuthenticationError, NotFoundError
from app.core.opaque_tokens import new_id, new_secret, secret_matches, split_token
from app.core.tokens import AccessToken, create_access_token
from app.models import AuthSession, User
from app.repositories.session_repository import SessionRepository
from app.services.auth_service import ensure_account_usable
from app.services.policy_service import ensure_device_allowed

SESSION_INVALID = "Tu sesión ya no es válida. Inicia sesión nuevamente."
SESSION_REPLACED = (
    "Se inició sesión con tu cuenta en otro dispositivo. Por seguridad, solo puedes tener una sesión activa a la vez."
)
#: Motivo de revocación cuando un inicio de sesión nuevo desplaza a uno anterior.
REPLACED_REASON = "SIGNED_IN_ELSEWHERE"


@dataclass(frozen=True)
class IssuedSession:
    session: AuthSession
    access: AccessToken
    #: Nuevo refresh token para la cookie; None si no rotó (ventana de gracia).
    refresh_token: str | None


class SessionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.sessions = SessionRepository(db)

    def create(self, user: User, *, ip: str | None, user_agent: str | None, persistent: bool = False) -> IssuedSession:
        """Nueva sesión. Un empleado con un solo empleo entra directo a su empresa (ya fijada en el usuario)."""
        now = datetime.now(UTC)
        secret = new_secret()
        session = AuthSession(
            id=new_id(),
            user_id=user.id,
            refresh_hash=hash_token(secret),
            # Vence junto con el access token: al cumplirse, hay que iniciar sesión de nuevo.
            expires_at=now + timedelta(minutes=settings.JWT_ACCESS_TTL_MINUTES),
            created_at=now,
            last_used_at=now,
            ip_address=ip,
            user_agent=(user_agent or "")[:255] or None,
            persistent=persistent,
            company_id=user.session_company_id,
        )
        self.sessions.add(session)
        self._enforce_session_limit(user.id, now, keep=session.id)
        self.sessions.purge_expired(now - timedelta(days=1))
        self.db.commit()
        return IssuedSession(session, self._access(session, user, now), f"{session.id}.{secret}")

    def refresh(self, refresh_token: str | None) -> IssuedSession:
        sid, secret = split_token(refresh_token)
        if not sid:
            raise AuthenticationError(SESSION_INVALID, code="REFRESH_TOKEN_MISSING")
        now = datetime.now(UTC)
        # FOR UPDATE: dos procesos no pueden rotar el mismo token a la vez.
        session = self.sessions.get(sid, for_update=True)
        if session is None:
            raise AuthenticationError(SESSION_INVALID, code="SESSION_INVALID")
        if not self._is_active(session, now):
            self._raise_inactive(session, "SESSION_INVALID")
        rotated: str | None = None
        if secret_matches(secret, session.refresh_hash):
            rotated = new_secret()
            session.previous_refresh_hash = session.refresh_hash
            session.refresh_hash = hash_token(rotated)
            session.rotated_at = now
        elif not self._within_grace(session, secret, now):
            # Token ya usado: alguien más lo tiene. Se revoca la sesión completa.
            session.revoked_at, session.revoked_reason = now, "REFRESH_REUSE_DETECTED"
            self.db.commit()
            raise AuthenticationError(SESSION_INVALID, code="REFRESH_TOKEN_REUSED")
        session.last_used_at = now
        self.db.commit()
        user = session.user
        user.use_company(session.company_id)
        ensure_account_usable(user)
        token = f"{session.id}.{rotated}" if rotated else None
        return IssuedSession(session, self._access(session, user, now), token)

    def select_company(self, session_id: str, user: User, company_id: int, headers: Mapping[str, str]) -> None:
        """EMPLOYEE en varias empresas: entra a una de ellas (queda fijada en su sesión).

        Solo a una empresa donde trabaje, con su empleo y la empresa activos, y desde un dispositivo
        que esa empresa permita.
        """
        if not any(e.company_id == company_id for e in user.employees):
            raise NotFoundError("No trabajas en esa empresa", code="COMPANY_NOT_FOUND")
        user.use_company(company_id)
        ensure_account_usable(user)
        ensure_device_allowed(self.db, user, headers)
        session = self.validate(session_id, user.id)
        session.company_id = company_id
        self.db.commit()

    def validate(self, session_id: str, user_id: int) -> AuthSession:
        """Usado en cada petición autenticada: la sesión debe existir y no estar revocada."""
        session = self.sessions.get(session_id)
        if session is None or session.user_id != user_id:
            raise AuthenticationError(SESSION_INVALID, code="SESSION_REVOKED")
        if not self._is_active(session, datetime.now(UTC)):
            self._raise_inactive(session, "SESSION_REVOKED")
        return session

    @staticmethod
    def _raise_inactive(session: AuthSession, code: str) -> NoReturn:
        """Sesión cerrada: si fue por un inicio de sesión en otro dispositivo, se dice así."""
        if session.revoked_reason == REPLACED_REASON:
            raise AuthenticationError(SESSION_REPLACED, code="SESSION_REPLACED")
        raise AuthenticationError(SESSION_INVALID, code=code)

    def revoke(self, session_id: str, user_id: int, reason: str = "LOGOUT") -> None:
        session = self.sessions.get(session_id)
        if session is None or session.user_id != user_id:
            raise NotFoundError("Sesión no encontrada", code="SESSION_NOT_FOUND")
        if session.revoked_at is None:
            session.revoked_at, session.revoked_reason = datetime.now(UTC), reason
        self.db.commit()

    def revoke_quietly(self, session_id: str, user_id: int, reason: str = "LOGOUT") -> None:
        """Revoca la sesión del access token si existe y es del usuario (sin error si no)."""
        session = self.sessions.get(session_id)
        if session is not None and session.user_id == user_id and session.revoked_at is None:
            session.revoked_at, session.revoked_reason = datetime.now(UTC), reason
            self.db.commit()

    def revoke_by_refresh_token(self, refresh_token: str | None) -> None:
        """Logout con la cookie: revoca solo si el secreto corresponde a la sesión."""
        sid, secret = split_token(refresh_token)
        session = self.sessions.get(sid) if sid else None
        if (
            session is not None
            and session.revoked_at is None
            and secret_matches(secret, session.refresh_hash, session.previous_refresh_hash)
        ):
            session.revoked_at, session.revoked_reason = datetime.now(UTC), "LOGOUT"
            self.db.commit()

    def revoke_all(self, user_id: int, reason: str, *, except_id: str | None = None, commit: bool = True) -> int:
        count = self.sessions.revoke_all(user_id, datetime.now(UTC), reason, except_id=except_id)
        if commit:
            self.db.commit()
        return count

    def revoke_company(self, user_id: int, company_id: int, reason: str) -> None:
        """Cierra las sesiones en las que la persona entró a esa empresa (sin confirmar la transacción)."""
        self.sessions.revoke_all(user_id, datetime.now(UTC), reason, company_id=company_id)

    def list_active(self, user_id: int) -> list[AuthSession]:
        return self.sessions.active_for_user(user_id, datetime.now(UTC))

    # ---------- Internos ----------

    @staticmethod
    def _access(session: AuthSession, user: User, now: datetime) -> AccessToken:
        return create_access_token(
            user_id=user.id,
            role=user.role.value,
            session_id=session.id,
            not_after=as_utc(session.expires_at),
            issued_at=now,
        )

    @staticmethod
    def _is_active(session: AuthSession, now: datetime) -> bool:
        expires_at = as_utc(session.expires_at)
        return session.revoked_at is None and expires_at is not None and expires_at > now

    @staticmethod
    def _within_grace(session: AuthSession, secret: str, now: datetime) -> bool:
        rotated_at = as_utc(session.rotated_at)
        return (
            secret_matches(secret, session.previous_refresh_hash)
            and rotated_at is not None
            and (now - rotated_at).total_seconds() <= settings.REFRESH_REUSE_GRACE_SECONDS
        )

    def _enforce_session_limit(self, user_id: int, now: datetime, *, keep: str) -> None:
        self.db.flush()
        active = self.sessions.active_for_user(user_id, now)
        for old in active[settings.MAX_SESSIONS_PER_USER :]:
            if old.id != keep:
                old.revoked_at, old.revoked_reason = now, REPLACED_REASON
