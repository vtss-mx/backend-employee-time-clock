from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import VerificationPolicy


class PolicyRepository:
    """Política de verificación de UNA empresa (una fila por empresa)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def get(self) -> VerificationPolicy | None:
        return self.db.scalar(select(VerificationPolicy).where(VerificationPolicy.company_id == self.company_id))

    def add(self, policy: VerificationPolicy) -> VerificationPolicy:
        self.db.add(policy)
        self.db.flush()
        return policy
