from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, UserRole


class UserRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, user_id: int) -> User | None:
        return self.db.get(User, user_id)

    # El correo se guarda siempre en minúsculas: la igualdad directa usa el índice único
    # (lower(email) en la consulta obligaría a recorrer la tabla en cada inicio de sesión).
    def get_by_email(self, email: str) -> User | None:
        return self.db.scalar(select(User).where(User.email == email.lower()))

    def email_exists(self, email: str, exclude_user_id: int | None = None) -> bool:
        stmt = select(User.id).where(User.email == email.lower())
        if exclude_user_id is not None:
            stmt = stmt.where(User.id != exclude_user_id)
        return self.db.scalar(stmt) is not None

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
