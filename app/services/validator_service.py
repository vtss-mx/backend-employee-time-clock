"""Validadores de identidad de una empresa (los administra COMPANY).

Cada validador es una cuenta (rol VALIDATOR) que inicia sesión en una tableta o un teléfono e
identifica a los empleados de la empresa según su modo. Nombre y modo viven en
workforce.validators; la cuenta (correo único, contraseña, activo) en auth.users.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.clock import business_day_start
from app.core.exceptions import ConflictError, NotFoundError
from app.core.passwords import hash_password
from app.models import UserRole, Validator
from app.repositories.company_repository import CompanyRepository
from app.repositories.user_repository import UserRepository
from app.repositories.validator_repository import ValidatorRepository
from app.schemas.validator import (
    ValidatorCreate,
    ValidatorPasswordReset,
    ValidatorRead,
    ValidatorUpdate,
)
from app.services.people_service import EMAIL_TAKEN


class ValidatorService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.validators = ValidatorRepository(db, company_id)
        self.users = UserRepository(db)

    def list_validators(self) -> list[ValidatorRead]:
        items = self.validators.list_all()
        today = self.validators.successes_since([v.user_id for v in items], business_day_start())
        return [self._to_read(v, today.get(v.user_id, 0)) for v in items]

    def get(self, validator_id: int) -> Validator:
        validator = self.validators.get(validator_id)
        if validator is None:
            raise NotFoundError("Validador no encontrado", code="VALIDATOR_NOT_FOUND")
        return validator

    def read(self, validator: Validator) -> ValidatorRead:
        today = self.validators.successes_since([validator.user_id], business_day_start())
        return self._to_read(validator, today.get(validator.user_id, 0))

    def create(self, data: ValidatorCreate) -> Validator:
        if self.users.email_exists(data.email):
            raise ConflictError(EMAIL_TAKEN, code="EMAIL_TAKEN", field="email")
        user = self.users.create(
            email=data.email,
            password_hash=hash_password(data.password),
            role=UserRole.VALIDATOR,
            company_id=self.company_id,
        )
        validator = self.validators.add(Validator(user_id=user.id, name=data.name, mode=data.mode))
        self.db.commit()
        self.db.refresh(validator)
        return validator

    def update(self, validator_id: int, data: ValidatorUpdate) -> Validator:
        validator = self.get(validator_id)
        for field, value in data.model_dump(exclude_unset=True, exclude_none=True).items():
            setattr(validator, field, value)
        self.db.commit()
        self.db.refresh(validator)
        return validator

    def set_active(self, validator_id: int, active: bool) -> Validator:
        validator = self.get(validator_id)
        validator.user.active = active
        if not active:
            self._close_sessions(validator, "ACCOUNT_DEACTIVATED")
        self.db.commit()
        self.db.refresh(validator)
        return validator

    def reset_password(self, validator_id: int, data: ValidatorPasswordReset) -> Validator:
        validator = self.get(validator_id)
        validator.user.password_hash = hash_password(data.password)
        self._close_sessions(validator, "PASSWORD_RESET")
        self.db.commit()
        return validator

    def delete(self, validator_id: int) -> None:
        """Elimina la cuenta del validador; su historial de identificaciones se conserva."""
        validator = self.get(validator_id)
        self.db.delete(validator.user)
        self.db.commit()

    def _close_sessions(self, validator: Validator, reason: str) -> None:
        CompanyRepository(self.db).revoke_sessions(datetime.now(UTC), reason, user_id=validator.user_id)

    @staticmethod
    def _to_read(validator: Validator, identifications_today: int) -> ValidatorRead:
        return ValidatorRead(
            id=validator.id,
            name=validator.name,
            email=validator.user.email,
            mode=validator.mode,
            active=validator.user.active,
            last_login_at=validator.user.last_login_at,
            identifications_today=identifications_today,
            created_at=validator.created_at,
        )
