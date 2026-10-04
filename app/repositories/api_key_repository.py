from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CompanyApiKey
from app.repositories.aggregates import paginate


class ApiKeyRepository:
    """Llaves de UNA empresa (una llave de otra empresa no existe para esta)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def page(self, *, offset: int, limit: int) -> tuple[list[CompanyApiKey], int]:
        stmt = select(CompanyApiKey).where(CompanyApiKey.company_id == self.company_id)
        return paginate(
            self.db, stmt, (CompanyApiKey.created_at.desc(), CompanyApiKey.id.desc()), offset=offset, limit=limit
        )

    def get(self, key_id: int) -> CompanyApiKey | None:
        return self.db.scalar(
            select(CompanyApiKey).where(CompanyApiKey.id == key_id, CompanyApiKey.company_id == self.company_id)
        )

    def count_usable(self) -> int:
        """Llaves sin revocar (las vencidas se cuentan hasta revocarlas: así se ven y se limpian)."""
        return (
            self.db.scalar(
                select(func.count())
                .select_from(CompanyApiKey)
                .where(CompanyApiKey.company_id == self.company_id, CompanyApiKey.revoked_at.is_(None))
            )
            or 0
        )

    def add(self, key: CompanyApiKey) -> CompanyApiKey:
        self.db.add(key)
        self.db.flush()
        return key


def find_by_hash(db: Session, key_hash: str) -> CompanyApiKey | None:
    """Autenticación: la llave por el hash de su secreto (la empresa sale de la llave)."""
    return db.scalar(select(CompanyApiKey).where(CompanyApiKey.key_hash == key_hash))
