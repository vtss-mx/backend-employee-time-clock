from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import VerificationPolicy


class PolicyRepository:
    """Política de verificación de UNA empresa (una fila por empresa)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def get(self, *, lock: bool = False) -> VerificationPolicy | None:
        """La política (`lock`: bloqueada hasta el commit, para que dos cambios no se pisen)."""
        stmt = select(VerificationPolicy).where(VerificationPolicy.company_id == self.company_id)
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return self.db.scalar(stmt)

    def add(self, policy: VerificationPolicy) -> VerificationPolicy:
        self.db.add(policy)
        self.db.flush()
        return policy
