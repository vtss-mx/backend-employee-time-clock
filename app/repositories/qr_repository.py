from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import EmployeeQr


class EmployeeQrRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_token_hash(self, token_hash: str) -> EmployeeQr | None:
        return self.db.scalar(select(EmployeeQr).where(EmployeeQr.token_hash == token_hash))

    def get_active_for_employee(self, employee_id: int) -> EmployeeQr | None:
        return self.db.scalar(
            select(EmployeeQr)
            .where(EmployeeQr.employee_id == employee_id, EmployeeQr.active.is_(True))
            .order_by(EmployeeQr.created_at.desc())
            .limit(1)
        )

    def employees_with_active_qr(self, employee_ids: Iterable[int]) -> set[int]:
        ids = list(employee_ids)
        if not ids:
            return set()
        rows = self.db.scalars(
            select(EmployeeQr.employee_id).where(EmployeeQr.employee_id.in_(ids), EmployeeQr.active.is_(True))
        ).all()
        return set(rows)

    def add(self, qr: EmployeeQr) -> EmployeeQr:
        self.db.add(qr)
        self.db.flush()
        return qr

    def revoke_all_for_employee(self, employee_id: int) -> None:
        self.db.execute(
            update(EmployeeQr)
            .where(EmployeeQr.employee_id == employee_id, EmployeeQr.active.is_(True))
            .values(active=False, revoked_at=datetime.now(UTC))
        )
