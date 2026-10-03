"""Validadores de identidad de una empresa (los administra COMPANY).

Cada validador es una cuenta (rol VALIDATOR) que inicia sesión en una tableta o un teléfono e
identifica a los empleados de la empresa según su modo. Nombre, modo, domicilio y ubicación
exigida viven en workforce.validators; la cuenta (correo único, contraseña, activo) en auth.users.

"Requiere ubicación": la cuenta solo inicia sesión dentro del radio del punto del domicilio
(lo valida `location_service` en el login). Exigirla o cambiar el punto o el radio cierra las
sesiones abiertas del validador: la siguiente se abre ya con la regla nueva.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.clock import business_day_start
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.core.passwords import hash_password
from app.models import DeviceStatus, SessionRevocationReason, UserRole, Validator
from app.repositories.company_repository import CompanyRepository
from app.repositories.user_repository import UserRepository
from app.repositories.validator_device_repository import ValidatorDeviceRepository
from app.repositories.validator_repository import ValidatorRepository
from app.schemas.address import Address
from app.schemas.common import PageParams
from app.schemas.validator import (
    ValidatorCreate,
    ValidatorList,
    ValidatorPasswordReset,
    ValidatorRead,
    ValidatorUpdate,
)
from app.services.people_service import EMAIL_TAKEN

LOCATION_POINT_REQUIRED = "Para exigir ubicación, marca en el mapa el punto del domicilio"
LOCATION_RADIUS_REQUIRED = "Indica el radio (en metros) dentro del cual puede iniciar sesión"
#: Columnas del domicilio en workforce.validators (mismos nombres que el esquema Address).
ADDRESS_FIELDS = tuple(Address.model_fields)


def _apply_address(validator: Validator, address: Address) -> None:
    for field in ADDRESS_FIELDS:
        setattr(validator, field, getattr(address, field))


def _location_rule(validator: Validator) -> tuple[bool, float | None, float | None, int | None]:
    return validator.location_required, validator.latitude, validator.longitude, validator.location_radius_m


def _ensure_location_complete(validator: Validator) -> None:
    """Exigir ubicación requiere el punto en el mapa y el radio (también lo garantiza un CHECK)."""
    if not validator.location_required:
        return
    if validator.latitude is None:
        raise UnprocessableError(LOCATION_POINT_REQUIRED, code="LOCATION_POINT_REQUIRED", field="address")
    if validator.location_radius_m is None:
        raise UnprocessableError(LOCATION_RADIUS_REQUIRED, code="LOCATION_RADIUS_REQUIRED", field="location_radius_m")


class ValidatorService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.validators = ValidatorRepository(db, company_id)
        self.users = UserRepository(db)

    def list_validators(self, page: PageParams) -> ValidatorList:
        items, total = self.validators.page(offset=page.offset, limit=page.size)
        today = self.validators.successes_since([v.user_id for v in items], business_day_start())
        devices = ValidatorDeviceRepository(self.db, self.company_id).counts([v.id for v in items])
        return ValidatorList.of(
            [self._to_read(v, today.get(v.user_id, 0), devices.get(v.id, {})) for v in items], total, page
        )

    def get(self, validator_id: int) -> Validator:
        validator = self.validators.get(validator_id)
        if validator is None:
            raise NotFoundError("Validador no encontrado", code="VALIDATOR_NOT_FOUND")
        return validator

    def read(self, validator: Validator) -> ValidatorRead:
        today = self.validators.successes_since([validator.user_id], business_day_start())
        devices = ValidatorDeviceRepository(self.db, self.company_id).counts([validator.id])
        return self._to_read(validator, today.get(validator.user_id, 0), devices.get(validator.id, {}))

    def create(self, data: ValidatorCreate) -> Validator:
        if self.users.email_exists(data.email):
            raise ConflictError(EMAIL_TAKEN, code="EMAIL_TAKEN", field="email")
        user = self.users.create(
            email=data.email,
            password_hash=hash_password(data.password),
            role=UserRole.VALIDATOR,
            company_id=self.company_id,
        )
        validator = Validator(
            user_id=user.id,
            name=data.name,
            mode=data.mode,
            location_required=data.location_required,
            location_radius_m=data.location_radius_m,
        )
        _apply_address(validator, data.address)
        _ensure_location_complete(validator)
        self.validators.add(validator)
        self.db.commit()
        self.db.refresh(validator)
        return validator

    def update(self, validator_id: int, data: ValidatorUpdate) -> Validator:
        validator = self.get(validator_id)
        before = _location_rule(validator)
        for field, value in data.model_dump(exclude_unset=True, exclude_none=True, exclude={"address"}).items():
            setattr(validator, field, value)
        if data.address is not None:
            _apply_address(validator, data.address)
        _ensure_location_complete(validator)
        # Ubicación recién exigida o con otro punto/radio: su sesión abierta no la cumplió.
        if validator.location_required and _location_rule(validator) != before:
            self._close_sessions(validator, SessionRevocationReason.LOCATION_POLICY_CHANGED)
        self.db.commit()
        self.db.refresh(validator)
        return validator

    def set_active(self, validator_id: int, active: bool) -> Validator:
        validator = self.get(validator_id)
        validator.user.active = active
        if not active:
            self._close_sessions(validator, SessionRevocationReason.ACCOUNT_DEACTIVATED)
        self.db.commit()
        self.db.refresh(validator)
        return validator

    def reset_password(self, validator_id: int, data: ValidatorPasswordReset) -> Validator:
        validator = self.get(validator_id)
        validator.user.password_hash = hash_password(data.password)
        self._close_sessions(validator, SessionRevocationReason.PASSWORD_RESET)
        self.db.commit()
        return validator

    def delete(self, validator_id: int) -> None:
        """Elimina la cuenta del validador; su historial de identificaciones se conserva."""
        validator = self.get(validator_id)
        self.db.delete(validator.user)
        self.db.commit()

    def _close_sessions(self, validator: Validator, reason: SessionRevocationReason) -> None:
        CompanyRepository(self.db).revoke_sessions(datetime.now(UTC), reason, user_id=validator.user_id)

    @staticmethod
    def _to_read(validator: Validator, identifications_today: int, devices: dict[str, int]) -> ValidatorRead:
        return ValidatorRead(
            id=validator.id,
            name=validator.name,
            email=validator.user.email,
            mode=validator.mode,
            active=validator.user.active,
            last_login_at=validator.user.last_login_at,
            identifications_today=identifications_today,
            created_at=validator.created_at,
            # Sin validar de nuevo (un país pudo desactivarse en el catálogo después de guardarlo).
            address=Address.model_construct(**{f: getattr(validator, f) for f in ADDRESS_FIELDS})
            if validator.street
            else None,
            location_required=validator.location_required,
            location_radius_m=validator.location_radius_m,
            devices_pending=devices.get(DeviceStatus.PENDING, 0),
            devices_approved=devices.get(DeviceStatus.APPROVED, 0),
        )
