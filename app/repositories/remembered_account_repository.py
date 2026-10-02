from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import RememberedAccount


class RememberedAccountRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, account_id: str) -> RememberedAccount | None:
        return self.db.get(RememberedAccount, account_id)

    def add(self, account: RememberedAccount) -> None:
        self.db.add(account)

    def delete(self, account: RememberedAccount) -> None:
        self.db.delete(account)

    def purge_expired(self, now: datetime) -> None:
        self.db.execute(
            delete(RememberedAccount)
            .where(RememberedAccount.expires_at <= now)
            .execution_options(synchronize_session=False)
        )
