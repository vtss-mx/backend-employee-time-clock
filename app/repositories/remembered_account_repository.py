from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import RememberedAccount, User
from app.repositories.aggregates import affected_rows


class RememberedAccountRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def forget_account(self, user_id: int) -> None:
        """La cuenta se elimina: ningún dispositivo la sigue recordando (dato técnico, se borra de verdad)."""
        affected_rows(self.db, delete(RememberedAccount).where(RememberedAccount.user_id == user_id))

    def forget_company_accounts(self, company_id: int) -> None:
        """Las cuentas de una empresa que se elimina: ningún dispositivo las sigue recordando (una sentencia)."""
        accounts = select(User.id).where(User.company_id == company_id)
        affected_rows(self.db, delete(RememberedAccount).where(RememberedAccount.user_id.in_(accounts)))

    def get(self, account_id: str) -> RememberedAccount | None:
        return self.db.get(RememberedAccount, account_id)

    def add(self, account: RememberedAccount) -> None:
        self.db.add(account)

    def delete(self, account: RememberedAccount) -> None:
        self.db.delete(account)
