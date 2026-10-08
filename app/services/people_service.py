"""Personas (cuentas) de la plataforma vistas desde UNA empresa.

- El correo y el teléfono son de la persona y únicos en toda la plataforma.
- Una persona puede trabajar en varias empresas con la misma cuenta: si otra empresa ya la
  registró, la nueva empresa la vincula (sin cambiar su contraseña) en lugar de duplicarla.
- Para vincular, el correo Y el teléfono deben coincidir con los de la cuenta: así una empresa no
  puede apropiarse de una cuenta ajena ni descubrir datos que no conoce.

Lo usan el alta de empleados y la validación en tiempo real (reglas y mensajes en un solo lugar).
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.i18n import LazyText, Text
from app.models import User, UserRole
from app.repositories.user_repository import UserRepository

#: Llaves de los mensajes (catálogo `app/i18n/messages/`): se traducen al responder, en el idioma de la petición.
EMAIL_TAKEN = "ACCOUNT_EMAIL_TAKEN"
ALREADY_EMPLOYEE = "ALREADY_EMPLOYEE"
PHONE_TAKEN = "ACCOUNT_PHONE_TAKEN"
PHONE_MISMATCH = "ACCOUNT_PHONE_MISMATCH"
NO_PHONE_TO_MATCH = "ACCOUNT_PHONE_MISSING"
LINKABLE_EMAIL = "ACCOUNT_LINKABLE_EMAIL"
LINKABLE_PHONE = "ACCOUNT_LINKABLE_PHONE"
PHONE_MATCHES = "ACCOUNT_PHONE_MATCHES"
SHARED_ACCOUNT = "SHARED_ACCOUNT"

#: MISMATCH: el teléfono no es el de la cuenta del correo escrito (no se puede vincular).
Match = Literal["AVAILABLE", "LINKABLE", "TAKEN", "MISMATCH"]


@dataclass(frozen=True)
class AccountCheck:
    match: Match
    #: Para la persona, diferido (se arma en el idioma de quien lo lee y, en el sobre, en cada idioma).
    text: LazyText | None = None


def ensure_account_free(users: UserRepository, account: User) -> None:
    """Al restaurar una cuenta de «Eliminados» (empleado, validador o las de una empresa): ninguna cuenta vigente usa ya
    su correo ni su teléfono (los dos son únicos en la plataforma entre las cuentas vigentes; 409 `RESTORE_CONFLICT`
    con el campo)."""
    if users.email_exists(account.email):
        raise ConflictError(
            code="RESTORE_CONFLICT", key="RESTORE_EMAIL_TAKEN", params={"email": account.email}, field="email"
        )
    if account.phone and users.get_by_phone(account.phone) is not None:
        raise ConflictError(
            code="RESTORE_CONFLICT", key="RESTORE_PHONE_TAKEN", params={"phone": account.phone}, field="phone"
        )


class PeopleService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.company_id = company_id
        self.users = UserRepository(db)

    def check_email(self, email: str, *, exclude_user_id: int | None = None) -> AccountCheck:
        owner = self.users.get_by_email(email)
        return self._check(owner, exclude_user_id, LINKABLE_EMAIL, taken=EMAIL_TAKEN, here=ALREADY_EMPLOYEE)

    def check_phone(self, phone: str, *, exclude_user_id: int | None = None) -> AccountCheck:
        owner = self.users.get_by_phone(phone)
        return self._check(owner, exclude_user_id, LINKABLE_PHONE, taken=PHONE_TAKEN, here=PHONE_TAKEN)

    def check_phone_for(self, phone: str, email: str) -> AccountCheck | None:
        """Teléfono junto con el correo escrito (alta): ambos deben ser de la misma persona para
        vincularla, con la misma regla que al guardar. None si el conflicto es del correo (lo
        informa su propio campo) y basta la verificación normal del teléfono."""
        try:
            account = self.account_to_link(email, phone)
        except ConflictError as exc:
            return AccountCheck("MISMATCH", exc.text) if exc.field == "phone" else None
        return AccountCheck("LINKABLE", Text(PHONE_MATCHES)) if account else AccountCheck("AVAILABLE")

    def account_to_link(self, email: str, phone: str) -> User | None:
        """Cuenta existente a vincular como empleado de esta empresa, o None si es una persona nueva.

        Conflictos: correo de un administrador o de alguien que ya trabaja aquí, teléfono de otra
        persona, o correo existente con otro teléfono.
        """
        account = self.users.get_by_email(email)
        phone_owner = self.users.get_by_phone(phone)
        if account is None:
            if phone_owner is not None:
                raise ConflictError(code="PHONE_TAKEN", key=PHONE_TAKEN, field="phone")
            return None
        if not self._linkable(account):
            key = ALREADY_EMPLOYEE if account.role == UserRole.EMPLOYEE else EMAIL_TAKEN
            raise ConflictError(code="EMAIL_TAKEN", key=key, field="email")
        if account.phone is None:
            raise ConflictError(code="ACCOUNT_PHONE_MISSING", key=NO_PHONE_TO_MATCH, field="phone")
        if phone_owner is None or phone_owner.id != account.id:
            raise ConflictError(code="ACCOUNT_PHONE_MISMATCH", key=PHONE_MISMATCH, field="phone")
        return account

    def _check(
        self, owner: User | None, exclude_user_id: int | None, linkable: str, *, taken: str, here: str
    ) -> AccountCheck:
        """AVAILABLE (libre o es la misma persona que se edita), LINKABLE (al dar de alta: persona de otra empresa) o
        TAKEN (administrador, alguien de esta empresa u otra persona al editar). `linkable`, `taken` y `here` son las
        llaves de sus mensajes."""
        if owner is None or owner.id == exclude_user_id:
            return AccountCheck("AVAILABLE")
        if self._linkable(owner):
            if exclude_user_id is None:
                return AccountCheck("LINKABLE", Text(linkable))
            return AccountCheck("TAKEN", Text(taken))
        return AccountCheck("TAKEN", Text(here if owner.role == UserRole.EMPLOYEE else taken))

    def _linkable(self, user: User) -> bool:
        """Empleado de OTRA(S) empresa(s): puede sumarse a esta con la misma cuenta."""
        return user.role == UserRole.EMPLOYEE and all(e.company_id != self.company_id for e in user.employees)
