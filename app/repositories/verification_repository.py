from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import VerificationLog


class VerificationLogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, log: VerificationLog) -> VerificationLog:
        self.db.add(log)
        self.db.flush()
        return log

    def list_for_employee(self, employee_id: int, limit: int = 20) -> list[VerificationLog]:
        return list(
            self.db.scalars(
                select(VerificationLog)
                .where(VerificationLog.employee_id == employee_id)
                .order_by(VerificationLog.created_at.desc(), VerificationLog.id.desc())
                .limit(limit)
            ).all()
        )
