from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, UserRole


class UserRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def emails_by_ids(self, user_ids: Iterable[int | None]) -> dict[int, str]:
        """Correo de varios usuarios en UNA consulta (quién revisó, creó o capturó en un listado)."""
        ids = {i for i in user_ids if i is not None}
        if not ids:
            return {}
        return {row.id: row.email for row in self.db.execute(select(User.id, User.email).where(User.id.in_(ids)))}

    def accounts_by_ids(self, user_ids: Iterable[int | None]) -> dict[int, tuple[str, UserRole]]:
        """Correo y rol de varios usuarios en UNA consulta (para decidir qué se puede mostrar)."""
        ids = {i for i in user_ids if i is not None}
        if not ids:
            return {}
        rows = self.db.execute(select(User.id, User.email, User.role).where(User.id.in_(ids)))
        return {row.id: (row.email, row.role) for row in rows}

    # El correo se guarda siempre en minúsculas: la igualdad directa usa el índice único
    # (lower(email) en la consulta obligaría a recorrer la tabla en cada inicio de sesión).
    def get_by_email(self, email: str) -> User | None:
        return self.db.scalar(select(User).where(User.email == email.lower()))

    def email_exists(self, email: str) -> bool:
        return self.db.scalar(select(User.id).where(User.email == email.lower())) is not None

    # Teléfono en E.164 (normalizado por el esquema): igualdad directa sobre su índice único.
    def get_by_phone(self, phone: str) -> User | None:
        return self.db.scalar(select(User).where(User.phone == phone))

    def create(
        self,
        *,
        email: str,
        password_hash: str,
        role: UserRole,
        company_id: int | None = None,
        phone: str | None = None,
        active: bool = True,
    ) -> User:
        user = User(
            email=email.lower(),
            password_hash=password_hash,
            role=role,
            company_id=company_id,
            phone=phone,
            active=active,
        )
        self.db.add(user)
        self.db.flush()
        return user
