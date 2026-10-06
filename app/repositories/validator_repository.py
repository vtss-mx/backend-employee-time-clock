from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.soft_delete import with_deleted
from app.models import User, UserRole, Validator, VerificationLog
from app.repositories.aggregates import affected_rows, get_scoped, group_counts, paginate, trash_page


class ValidatorRepository:
    """Validadores de UNA empresa (un id de otra empresa se comporta como inexistente). Con borrado lógico: solo los
    vigentes, salvo la papelera, eliminar/restaurar y quién hizo cada identificación (historial)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def page(self, *, offset: int, limit: int, deleted: bool = False) -> tuple[list[Validator], int]:
        """Los vigentes en orden alfabético o, con `deleted`, la papelera (el eliminado más reciente primero)."""
        stmt = select(Validator).where(Validator.company_id == self.company_id)
        if deleted:
            return trash_page(self.db, stmt, Validator, offset=offset, limit=limit)
        return paginate(self.db, stmt, (func.lower(Validator.name), Validator.id), offset=offset, limit=limit)

    def get(self, validator_id: int, *, lock: bool = False, include_deleted: bool = False) -> Validator | None:
        return get_scoped(self.db, Validator, validator_id, self.company_id, lock=lock, include_deleted=include_deleted)

    def active_count(self) -> int:
        """Validadores ACTIVOS de la empresa: los que cuentan contra su límite (`companies.max_validators`) y en el
        cobro. Se cuentan sus cuentas (rol VALIDATOR, una por validador) por el índice `(company_id, role)`."""
        stmt = select(func.count()).where(
            User.company_id == self.company_id, User.role == UserRole.VALIDATOR, User.active.is_(True)
        )
        return int(self.db.scalar(stmt) or 0)

    def by_user_ids(self, user_ids: set[int]) -> dict[int, Validator]:
        """Validadores de la empresa por su cuenta de usuario (quién hizo cada identificación): referencias del
        historial, también los que están en «Eliminados»."""
        if not user_ids:
            return {}
        stmt = select(Validator).where(Validator.company_id == self.company_id, Validator.user_id.in_(user_ids))
        return {v.user_id: v for v in self.db.scalars(with_deleted(stmt))}

    def active_ids(self) -> list[int]:
        """Los validadores vigentes con su cuenta activa (los que hoy se cobran): eliminar o restaurar la empresa deja
        de cobrarlos o los vuelve a cobrar."""
        stmt = (
            select(Validator.id)
            .join(User, User.id == Validator.user_id)
            .where(Validator.company_id == self.company_id, User.company_id == self.company_id, User.active.is_(True))
        )
        return list(self.db.scalars(stmt.order_by(Validator.id)))

    def delete_all(self, at: datetime, by: str) -> None:
        """Los validadores vigentes de la empresa a «Eliminados» con la marca de la empresa (una sentencia)."""
        stmt = update(Validator).where(Validator.company_id == self.company_id).values(deleted_at=at, deleted_by=by)
        affected_rows(self.db, stmt)

    def restore_all(self, at: datetime) -> None:
        """Los validadores que se eliminaron con la empresa (misma marca) vuelven (una sentencia)."""
        stmt = update(Validator).where(Validator.company_id == self.company_id, Validator.deleted_at == at)
        affected_rows(self.db, with_deleted(stmt.values(deleted_at=None, deleted_by=None)))

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
