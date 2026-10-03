from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Validator, VerificationLog
from app.repositories.aggregates import group_counts, paginate


class ValidatorRepository:
    """Validadores de UNA empresa (un id de otra empresa se comporta como inexistente)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def page(self, *, offset: int, limit: int) -> tuple[list[Validator], int]:
        stmt = select(Validator).where(Validator.company_id == self.company_id)
        return paginate(self.db, stmt, (func.lower(Validator.name), Validator.id), offset=offset, limit=limit)

    def get(self, validator_id: int) -> Validator | None:
        validator = self.db.get(Validator, validator_id)
        return validator if validator is not None and validator.company_id == self.company_id else None

    def add(self, validator: Validator) -> Validator:
        validator.company_id = self.company_id
        self.db.add(validator)
        self.db.flush()
        return validator

    def successes_since(self, user_ids: list[int], since: datetime) -> dict[int, int]:
        """Identificaciones exitosas por cuenta de validador desde `since`."""
        if not user_ids:
            return {}
        return group_counts(
            self.db,
            select(VerificationLog.user_id, func.count())
            .where(
                VerificationLog.user_id.in_(user_ids),
                VerificationLog.success.is_(True),
                VerificationLog.created_at >= since,
            )
            .group_by(VerificationLog.user_id),
        )
