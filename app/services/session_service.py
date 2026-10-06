"""Sesiones de usuario: emisión, rotación de refresh tokens y revocación.

Refresh token = "<sid>.<secreto aleatorio de 256 bits>". Solo se guarda el SHA-256 del
secreto. En cada /auth/refresh el secreto rota; si llega uno ya rotado (fuera de la ventana
de gracia para pestañas concurrentes) se asume robo y se revoca la sesión completa.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.crypto import hash_token
from app.core.exceptions import AuthenticationError, NotFoundError
from app.core.opaque_tokens import new_id, new_secret, next_secret, secret_matches, split_token
from app.core.tokens import AccessToken, create_access_token, decode_access_token
from app.models import AuthSession, SessionRevocationReason, User
from app.repositories.session_repository import SessionRepository
from app.schemas.common import PageParams
from app.services.auth_service import AuthService, ensure_account_usable
from app.services.catalog_service import get_catalogs
from app.services.policy_service import ensure_device_allowed


def session_closed(reason: str | None, code: str) -> AuthenticationError:
    """401 de una sesión inválida o cerrada: el mensaje depende del motivo del cierre
    (catalog.session_revocation_reasons). Si fue un inicio de sesión en otro dispositivo, se dice así."""
    if reason == SessionRevocationReason.SIGNED_IN_ELSEWHERE:
        code = "SESSION_REPLACED"
    elif reason == SessionRevocationReason.COMPANY_SUSPENDED:
        # La app muestra la pantalla de empresa suspendida (el mismo código del 403 al iniciar sesión).
        code = "COMPANY_SUSPENDED"
    return AuthenticationError(get_catalogs().session_message(reason), code=code)


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

    def create(
        self,
        user: User,
        *,
        ip: str | None,
        user_agent: str | None,
        persistent: bool = False,
        device_key_hash: str | None = None,
    ) -> IssuedSession:
        """Nueva sesión. Un empleado con un solo empleo entra directo a su empresa (ya fijada en el usuario). Un
        validador que probó su dispositivo queda ligado a esa llave (`device_key_hash`, antifraude 2b)."""
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
            device_key_hash=device_key_hash,
        )
        self.sessions.add(session)
        self._enforce_session_limit(user.id, now, keep=session.id)
        self.db.commit()
        return IssuedSession(session, self._access(session, user, now), f"{session.id}.{secret}")

    def refresh(self, refresh_token: str | None) -> IssuedSession:
        sid, secret = split_token(refresh_token)
        if not sid:
            raise session_closed(None, "REFRESH_TOKEN_MISSING")
        now = datetime.now(UTC)
        # FOR UPDATE: dos procesos no pueden rotar el mismo token a la vez.
        session = self.sessions.get(sid, for_update=True)
        if session is None:
            raise session_closed(None, "SESSION_INVALID")
        if not self._is_active(session, now):
            raise session_closed(session.revoked_reason, "SESSION_INVALID")
        rotated: str | None = None
        if secret_matches(secret, session.refresh_hash):
            rotated = next_secret(session.id, secret)
            session.previous_refresh_hash = session.refresh_hash
            session.refresh_hash = hash_token(rotated)
            session.rotated_at = now
        elif self._within_grace(session, secret, now):
            # Reintento con el token anterior (p. ej. se perdió la respuesta): el mismo siguiente
            # secreto, si sigue vigente, vuelve a entregarse en la cookie.
            again = next_secret(session.id, secret)
            rotated = again if secret_matches(again, session.refresh_hash) else None
        else:
            # Token ya usado: alguien más lo tiene. Se revoca la sesión completa.
            session.revoked_at, session.revoked_reason = now, SessionRevocationReason.REFRESH_REUSE_DETECTED
            self.db.commit()
            raise session_closed(session.revoked_reason, "REFRESH_TOKEN_REUSED")
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
            raise NotFoundError(code="COMPANY_NOT_FOUND", key="NOT_YOUR_COMPANY")
        user.use_company(company_id)
        ensure_account_usable(user)
        ensure_device_allowed(self.db, user, headers)
        session = self.validate(session_id, user.id)
        session.company_id = company_id
        self.db.commit()

    def authenticate_access(self, token: str, headers: Mapping[str, str]) -> tuple[User, str]:
        """El usuario de un access token, para CADA petición (HTTP y canal WebSocket): JWT válido,
        sesión activa, el mismo rol con el que se emitió, cuenta utilizable y dispositivo permitido.
        Revisar la sesión siempre (no solo el JWT) hace que cerrar sesión, revocarla o desactivar la
        cuenta surta efecto de inmediato. Devuelve el usuario y el id de la sesión."""
        payload = decode_access_token(token)
        try:
            user_id, session_id = int(payload["sub"]), str(payload["sid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError(code="TOKEN_INVALID") from exc
        user = self.active_user(session_id, user_id)
        if payload.get("role") != user.role.value:
            raise AuthenticationError(code="TOKEN_INVALID", key="SESSION_NO_LONGER_VALID")
        # En cada petición (no solo al iniciar sesión): un token obtenido en un dispositivo
        # permitido tampoco sirve desde otro.
        ensure_device_allowed(self.db, user, headers)
        return user, session_id

    def active_user(self, session_id: str, user_id: int) -> User:
        """Usuario de una sesión activa, operando en la empresa que fija la sesión (un empleado en
        varias empresas), con su cuenta, empleo y empresa todavía activos."""
        session = self.validate(session_id, user_id)
        user = session.user
        user.use_company(session.company_id)
        user.use_device_key(session.device_key_hash)
        ensure_account_usable(user)
        return user

    def validate(self, session_id: str, user_id: int) -> AuthSession:
        """Usado en cada petición autenticada: la sesión debe existir y no estar revocada."""
        session = self.sessions.get(session_id)
        if session is None or session.user_id != user_id:
            raise session_closed(None, "SESSION_REVOKED")
        if not self._is_active(session, datetime.now(UTC)):
            raise session_closed(session.revoked_reason, "SESSION_REVOKED")
        return session

    def revoke(
        self, session_id: str, user_id: int, reason: SessionRevocationReason = SessionRevocationReason.LOGOUT
    ) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.user_id != user_id:
            raise NotFoundError(code="SESSION_NOT_FOUND")
        self._close(session, reason)
        self.db.commit()

    def revoke_quietly(
        self, session_id: str, user_id: int, reason: SessionRevocationReason = SessionRevocationReason.LOGOUT
    ) -> None:
        """Revoca la sesión del access token si existe y es del usuario (sin error si no)."""
        session = self.sessions.get(session_id)
        if session is not None and session.user_id == user_id and self._close(session, reason):
            self.db.commit()

    def has_session(self, refresh_token: str | None) -> bool:
        """¿La cookie corresponde a una sesión vigente? Solo consulta: no rota el token ni la cierra.

        Así el navegador decide al cargar si restaura la sesión sin guardar nada propio (ni
        localStorage): la cookie HttpOnly es la única fuente."""
        sid, secret = split_token(refresh_token)
        session = self.sessions.get(sid) if sid else None
        return (
            session is not None
            and self._is_active(session, datetime.now(UTC))
            and secret_matches(secret, session.refresh_hash, session.previous_refresh_hash)
        )

    def revoke_by_refresh_token(self, refresh_token: str | None) -> None:
        """Logout con la cookie: revoca solo si el secreto corresponde a la sesión."""
        sid, secret = split_token(refresh_token)
        session = self.sessions.get(sid) if sid else None
        if (
            session is not None
            and secret_matches(secret, session.refresh_hash, session.previous_refresh_hash)
            and self._close(session, SessionRevocationReason.LOGOUT)
        ):
            self.db.commit()

    def revoke_all(
        self, user_id: int, reason: SessionRevocationReason, *, except_id: str | None = None, commit: bool = True
    ) -> int:
        """Cierra las sesiones de una cuenta. Con `commit=False` va dentro de la transacción de quien
        la llama (desactivar, restablecer la contraseña, revocar un dispositivo...): el cambio y el
        cierre se confirman juntos o ninguno."""
        count = self.sessions.revoke_all(user_id, datetime.now(UTC), reason, except_id=except_id)
        if commit:
            self.db.commit()
        return count

    def revoke_company(self, user_id: int, company_id: int, reason: SessionRevocationReason) -> None:
        """Cierra las sesiones en las que la persona entró a esa empresa (sin confirmar la transacción)."""
        self.sessions.revoke_all(user_id, datetime.now(UTC), reason, company_id=company_id)

    def close_company(self, company_id: int, reason: SessionRevocationReason) -> None:
        """Corta el acceso de todo el personal de una empresa (sin confirmar la transacción)."""
        self.sessions.revoke_company(company_id, datetime.now(UTC), reason)

    def change_password(self, user: User, current_password: str, new_password: str, *, keep: str | None) -> int:
        """Cambia la contraseña y cierra las demás sesiones (otros dispositivos) en UNA transacción:
        no puede quedar la contraseña nueva con las sesiones viejas abiertas. Devuelve cuántas cerró."""
        AuthService(self.db).change_password(user, current_password, new_password)
        return self.revoke_all(user.id, SessionRevocationReason.PASSWORD_CHANGED, except_id=keep)

    def page_active(self, user_id: int, page: PageParams) -> tuple[list[AuthSession], int]:
        return self.sessions.page_active(user_id, datetime.now(UTC), offset=page.offset, limit=page.size)

    # ---------- Internos ----------

    @staticmethod
    def _close(session: AuthSession, reason: SessionRevocationReason) -> bool:
        """Marca una sesión como cerrada (con su motivo); False si ya lo estaba."""
        if session.revoked_at is not None:
            return False
        session.revoked_at, session.revoked_reason = datetime.now(UTC), reason
        return True

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
                old.revoked_at, old.revoked_reason = now, SessionRevocationReason.SIGNED_IN_ELSEWHERE
