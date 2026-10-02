"""\"Recordar mi cuenta\": qué cuenta mostrar ya escrita en el login de un dispositivo."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.crypto import hash_token
from app.core.exceptions import AuthenticationError
from app.core.opaque_tokens import new_id, new_secret, secret_matches, split_token
from app.models import RememberedAccount, User
from app.repositories.remembered_account_repository import RememberedAccountRepository
from app.services.auth_service import ensure_account_usable


class RememberedAccountService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.accounts = RememberedAccountRepository(db)

    def lookup(self, token: str | None) -> RememberedAccount | None:
        """Cuenta recordada en el dispositivo (solo si el secreto coincide, no venció y sigue activa)."""
        account_id, secret = split_token(token)
        account = self.accounts.get(account_id) if account_id else None
        if (
            account is None
            or not secret_matches(secret, account.token_hash)
            or as_utc(account.expires_at) <= datetime.now(UTC)
        ):
            return None
        try:
            ensure_account_usable(account.user)  # la misma regla que al iniciar sesión
        except AuthenticationError:
            return None
        return account

    def remember(self, user: User, token: str | None) -> str:
        """Recuerda la cuenta en el dispositivo (reutiliza su registro) y devuelve la cookie nueva."""
        now = datetime.now(UTC)
        account = self.lookup(token)
        if account is None:
            account = RememberedAccount(id=new_id(), created_at=now)
            self.accounts.add(account)
        secret = new_secret()  # rota en cada inicio de sesión
        account.user_id = user.id
        account.token_hash = hash_token(secret)
        account.last_used_at = now
        account.expires_at = now + timedelta(days=settings.REMEMBER_ACCOUNT_DAYS)
        self.accounts.purge_expired(now)
        self.db.commit()
        return f"{account.id}.{secret}"

    def forget(self, token: str | None) -> None:
        account = self.lookup(token)
        if account is not None:
            self.accounts.delete(account)
            self.db.commit()
