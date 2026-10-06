from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.soft_delete import with_deleted
from app.models import User, UserRole
from app.repositories.aggregates import affected_rows


class UserRepository:
    """Cuentas de la plataforma. Con borrado lógico: el inicio de sesión, la validación en vivo y los datos únicos ven
    solo las vigentes; los correos del historial (`emails_by_ids`, `accounts_by_ids`) también los de las eliminadas."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def emails_by_ids(self, user_ids: Iterable[int | None]) -> dict[int, str]:
        """Correo de varios usuarios en UNA consulta (quién revisó, creó o capturó en un listado): referencias del
        historial, también de cuentas en «Eliminados»."""
        ids = {i for i in user_ids if i is not None}
        if not ids:
            return {}
        rows = self.db.execute(with_deleted(select(User.id, User.email).where(User.id.in_(ids))))
        return {row.id: row.email for row in rows}

    def accounts_by_ids(self, user_ids: Iterable[int | None]) -> dict[int, tuple[str, UserRole]]:
        """Correo y rol de varios usuarios en UNA consulta (para decidir qué se puede mostrar); también de cuentas en
        «Eliminados» (referencias del historial)."""
        ids = {i for i in user_ids if i is not None}
        if not ids:
            return {}
        rows = self.db.execute(with_deleted(select(User.id, User.email, User.role).where(User.id.in_(ids))))
        return {row.id: (row.email, row.role) for row in rows}

    # ---------- Cuentas de una empresa (eliminarla y restaurarla las lleva consigo) ----------

    def delete_company_accounts(self, company_id: int, at: datetime, by: str) -> int:
        """Manda a «Eliminados» las cuentas vigentes de la empresa (administradores y validadores) con la MISMA marca
        de la empresa: restaurarla regresa exactamente las que se fueron con ella. Una sentencia."""
        stmt = update(User).where(User.company_id == company_id).values(deleted_at=at, deleted_by=by)
        return affected_rows(self.db, stmt)

    def deleted_with(self, company_id: int, at: datetime) -> list[User]:
        """Las cuentas de la empresa que se eliminaron con ella (misma marca de tiempo)."""
        stmt = select(User).where(User.company_id == company_id, User.deleted_at == at)
        return list(self.db.scalars(with_deleted(stmt).order_by(User.id)))

    def restore_company_accounts(self, company_id: int, at: datetime) -> int:
        stmt = update(User).where(User.company_id == company_id, User.deleted_at == at)
        return affected_rows(self.db, with_deleted(stmt.values(deleted_at=None, deleted_by=None)))

    def clear_company_avatars(self, company_id: int) -> None:
        """Sin foto de perfil (sus objetos ya van a la cola de borrado): las iniciales en su lugar."""
        affected_rows(self.db, update(User).where(User.company_id == company_id).values(avatar_version=None))

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
