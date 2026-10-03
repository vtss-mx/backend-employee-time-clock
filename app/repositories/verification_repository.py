from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import VerificationLog
from app.repositories.aggregates import paginate


class VerificationLogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, log: VerificationLog) -> VerificationLog:
        self.db.add(log)
        self.db.flush()
        return log

    def page_for_employee(self, employee_id: int, *, offset: int, limit: int) -> tuple[list[VerificationLog], int]:
        """Bitácora del empleado, la más reciente primero (índice employee_id + created_at)."""
        stmt = select(VerificationLog).where(VerificationLog.employee_id == employee_id)
        order = (VerificationLog.created_at.desc(), VerificationLog.id.desc())
        return paginate(self.db, stmt, order, offset=offset, limit=limit)
