"""Validadores de identidad de una empresa (los administra COMPANY).

Cada validador es una cuenta (rol VALIDATOR) que inicia sesión en una tableta o un teléfono e
identifica a los empleados de la empresa según su modo. Nombre, modo, domicilio y ubicación
exigida viven en workforce.validators; la cuenta (correo único, contraseña, activo) en auth.users.

"Requiere ubicación": la cuenta solo inicia sesión dentro del radio del punto del domicilio
(lo valida `location_service` en el login). Exigirla o cambiar el punto o el radio cierra las
sesiones abiertas del validador: la siguiente se abre ya con la regla nueva.

Módulo que concede el ADMIN (decisión del dueño del producto): la empresa tiene validadores solo si su límite
(`companies.max_validators`) es mayor que cero, y nunca más ACTIVOS que ese límite. Dar de alta o activar uno
revisa el límite con la empresa bloqueada (el candado de la cobranza: sin carreras entre dos altas ni con el ADMIN
cambiando el límite). Cada validador activo cuenta como un empleado en el cobro: su alta, (des)activación y
eliminación escriben su evento (`workforce.validator_status_events`) en la misma transacción.

Eliminar es un borrado lógico (regla 20 de la raíz) de su configuración y su cuenta: va a «Eliminados» con su historial
de identificaciones; su foto de perfil se borra de verdad (`person_erasure`). Restaurar revisa de nuevo su correo y el
límite de validadores activos.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.clock import business_day_start
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, UnprocessableError
from app.core.passwords import hash_password
from app.models import Company, DeviceStatus, SessionRevocationReason, User, UserRole, Validator
from app.repositories.billing_repository import BillingRepository
from app.repositories.user_repository import UserRepository
from app.repositories.validator_device_repository import ValidatorDeviceRepository
from app.repositories.validator_repository import ValidatorRepository
from app.schemas.address import address_of, apply_address
from app.schemas.avatar import person_avatar
from app.schemas.common import PageParams, deletion_of
from app.schemas.validator import (
    ValidatorCreate,
    ValidatorList,
    ValidatorPasswordReset,
    ValidatorRead,
    ValidatorUpdate,
)
from app.services.billing_ledger import Ledger
from app.services.people_service import EMAIL_TAKEN, ensure_account_free
from app.services.person_erasure import erase_account
from app.services.session_service import SessionService
from app.services.trash import commit_restore, ensure_deleted, ensure_live


def validators_module(user: User) -> Company:
    """La empresa en que opera el usuario, con el módulo de validadores (un límite mayor que cero, lo fija el ADMIN).
    Sin él, 403 VALIDATORS_DISABLED en cada API que administra validadores (y la pantalla no aparece:
    `navigation_service.AVAILABILITY`): una sola regla para la dependencia del router y la validación en vivo."""
    company = user.current_company
    if company is None or not company.validators_enabled:
        raise PermissionDeniedError(code="VALIDATORS_DISABLED")
    return company


def _location_rule(validator: Validator) -> tuple[bool, float | None, float | None, int | None]:
    return validator.location_required, validator.latitude, validator.longitude, validator.location_radius_m


def _ensure_location_complete(validator: Validator) -> None:
    """Exigir ubicación requiere el punto en el mapa y el radio (también lo garantiza un CHECK)."""
    if not validator.location_required:
        return
    if validator.latitude is None:
        raise UnprocessableError(code="LOCATION_POINT_REQUIRED", field="address")
    if validator.location_radius_m is None:
        raise UnprocessableError(code="LOCATION_RADIUS_REQUIRED", field="location_radius_m")


class ValidatorService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.validators = ValidatorRepository(db, company_id)
        self.users = UserRepository(db)

    def list_validators(self, page: PageParams, limit: int, *, deleted: bool = False) -> ValidatorList:
        """Una página de validadores con el uso del límite: cuántos activos y cuántos permite (`limit`, el de la
        empresa autenticada). Con `deleted`, la papelera (el eliminado más reciente primero)."""
        items, total = self.validators.page(offset=page.offset, limit=page.size, deleted=deleted)
        today = self.validators.successes_since([v.user_id for v in items], business_day_start())
        devices = ValidatorDeviceRepository(self.db, self.company_id).counts([v.id for v in items])
        return ValidatorList.of(
            [self._to_read(v, today.get(v.user_id, 0), devices.get(v.id, {})) for v in items],
            total,
            page,
            active=self.validators.active_count(),
            limit=limit,
        )

    def get(self, validator_id: int, *, include_deleted: bool = False, lock: bool = False) -> Validator:
        """El validador vigente (404 si no existe, es de otra empresa o está en «Eliminados»); con `include_deleted`,
        también uno eliminado (su detalle con la marca, eliminarlo y restaurarlo)."""
        validator = self.validators.get(validator_id, lock=lock, include_deleted=include_deleted)
        if validator is None:
            raise NotFoundError(code="VALIDATOR_NOT_FOUND")
        return validator

    def read(self, validator: Validator) -> ValidatorRead:
        today = self.validators.successes_since([validator.user_id], business_day_start())
        devices = ValidatorDeviceRepository(self.db, self.company_id).counts([validator.id])
        return self._to_read(validator, today.get(validator.user_id, 0), devices.get(validator.id, {}))

    def create(self, data: ValidatorCreate) -> Validator:
        # Argon2 es CPU: se calcula antes de abrir la transacción (y del candado de la empresa).
        password_hash = hash_password(data.password)
        if self.users.email_exists(data.email):
            raise ConflictError(code="EMAIL_TAKEN", key=EMAIL_TAKEN, field="email")
        self._claim_seat()
        user = self.users.create(
            email=data.email,
            password_hash=password_hash,
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
        apply_address(validator, data.address)
        _ensure_location_complete(validator)
        self.validators.add(validator)
        # Cuenta para el cobro como un empleado desde este momento.
        BillingRepository(self.db).record_validator_status(self.company_id, validator.id, True)
        self.db.commit()
        self.db.refresh(validator)
        return validator

    def update(self, validator_id: int, data: ValidatorUpdate) -> Validator:
        validator = self.get(validator_id)
        before = _location_rule(validator)
        for field, value in data.model_dump(exclude_unset=True, exclude_none=True, exclude={"address"}).items():
            setattr(validator, field, value)
        if data.address is not None:
            apply_address(validator, data.address)
        _ensure_location_complete(validator)
        # Ubicación recién exigida o con otro punto/radio: su sesión abierta no la cumplió.
        if validator.location_required and _location_rule(validator) != before:
            self._close_sessions(validator, SessionRevocationReason.LOCATION_POLICY_CHANGED)
        self.db.commit()
        self.db.refresh(validator)
        return validator

    def set_active(self, validator_id: int, active: bool) -> Validator:
        """Activar ocupa un lugar del límite (409 VALIDATOR_LIMIT_REACHED si no queda); desactivar lo libera y cierra
        sus sesiones. Cada cambio real cuenta para el cobro desde este momento (su evento)."""
        validator = self.get(validator_id)
        if validator.user.active != active:
            if active:
                self._claim_seat()
            BillingRepository(self.db).record_validator_status(self.company_id, validator.id, active)
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

    def delete(self, validator_id: int, actor: User) -> None:
        """Manda al validador y su cuenta a «Eliminados» (borrado lógico): sus sesiones se cierran, deja de contar
        contra el límite y en el cobro desde hoy y su correo queda libre; su historial de identificaciones se
        conserva y su foto de perfil se borra de verdad."""
        validator = self.get(validator_id, include_deleted=True, lock=True)
        ensure_live(validator)
        if validator.user.active:  # deja de contar para el cobro (los días que estuvo activo se conservan)
            BillingRepository(self.db).record_validator_status(self.company_id, validator.id, False)
        self._close_sessions(validator, SessionRevocationReason.ACCOUNT_DELETED)
        erase_account(self.db, validator.user)
        now = datetime.now(UTC)
        validator.mark_deleted(now, actor.email)
        validator.user.mark_deleted(now, actor.email)
        self.db.commit()

    def restore(self, validator_id: int) -> Validator:
        """Regresa al validador de «Eliminados»: su correo debe seguir libre (409 `RESTORE_CONFLICT`) y, si estaba
        activo, ocupa otra vez un lugar del límite (409 `VALIDATOR_LIMIT_REACHED`) y cuenta para el cobro desde hoy.
        Su foto de perfil no regresa."""
        validator = self.get(validator_id, include_deleted=True, lock=True)
        ensure_deleted(validator)
        user = validator.user
        ensure_account_free(self.users, user)
        if user.active:
            self._claim_seat()
            BillingRepository(self.db).record_validator_status(self.company_id, validator.id, True)
        validator.mark_restored()
        user.mark_restored()
        commit_restore(self.db)
        self.db.refresh(validator)
        return validator

    def _claim_seat(self) -> None:
        """Un validador activo más, dentro del límite que fijó el ADMIN. Se cuenta con la empresa bloqueada: dos altas
        a la vez (o un alta mientras el ADMIN baja el límite) se turnan y la segunda ve a la primera."""
        limit = Ledger(self.db).lock(self.company_id).max_validators
        if self.validators.active_count() >= limit:
            raise ConflictError(code="VALIDATOR_LIMIT_REACHED", params={"count": limit}, details={"limit": limit})

    def _close_sessions(self, validator: Validator, reason: SessionRevocationReason) -> None:
        SessionService(self.db).revoke_all(validator.user_id, reason, commit=False)

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
            address=address_of(validator),
            location_required=validator.location_required,
            location_radius_m=validator.location_radius_m,
            devices_pending=devices.get(DeviceStatus.PENDING, 0),
            devices_approved=devices.get(DeviceStatus.APPROVED, 0),
            # La cuenta viene con el validador (JOIN de su carga): sin consultas de más.
            avatar=person_avatar(validator.user_id, validator.user.avatar_version, deleted=validator.deleted),
            **deletion_of(validator),
        )
