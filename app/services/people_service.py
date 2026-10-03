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
from app.models import User, UserRole
from app.repositories.user_repository import UserRepository

EMAIL_TAKEN = "El correo electrónico ya está registrado"
ALREADY_EMPLOYEE = "Esta persona ya es empleado de tu empresa"
PHONE_TAKEN = "El teléfono ya está registrado en otra cuenta"
PHONE_MISMATCH = "Ese correo ya tiene una cuenta con otro teléfono. Verifica el teléfono de la persona."
NO_PHONE_TO_MATCH = (
    "La cuenta de esta persona aún no tiene teléfono registrado; su empresa actual debe capturarlo antes de vincularla."
)
LINKABLE_EMAIL = (
    "Esta persona ya tiene cuenta en Employee Time Clock: se agregará a tu empresa con su misma cuenta y contraseña."
)
LINKABLE_PHONE = "Este teléfono pertenece a una cuenta existente: el correo debe ser el de esa misma persona."
PHONE_MATCHES = "El teléfono coincide con la cuenta de esta persona: se vinculará a tu empresa."
SHARED_ACCOUNT = (
    "La cuenta de esta persona también pertenece a otra empresa: su correo, su teléfono y su contraseña "
    "no se pueden cambiar desde aquí."
)

#: MISMATCH: el teléfono no es el de la cuenta del correo escrito (no se puede vincular).
Match = Literal["AVAILABLE", "LINKABLE", "TAKEN", "MISMATCH"]


@dataclass(frozen=True)
class AccountCheck:
    match: Match
    message: str | None = None


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
            return AccountCheck("MISMATCH", exc.message) if exc.field == "phone" else None
        return AccountCheck("LINKABLE", PHONE_MATCHES) if account else AccountCheck("AVAILABLE")

    def account_to_link(self, email: str, phone: str) -> User | None:
        """Cuenta existente a vincular como empleado de esta empresa, o None si es una persona nueva.

        Conflictos: correo de un administrador o de alguien que ya trabaja aquí, teléfono de otra
        persona, o correo existente con otro teléfono.
        """
        account = self.users.get_by_email(email)
        phone_owner = self.users.get_by_phone(phone)
        if account is None:
            if phone_owner is not None:
                raise ConflictError(PHONE_TAKEN, code="PHONE_TAKEN", field="phone")
            return None
        if not self._linkable(account):
            message = ALREADY_EMPLOYEE if account.role == UserRole.EMPLOYEE else EMAIL_TAKEN
            raise ConflictError(message, code="EMAIL_TAKEN", field="email")
        if account.phone is None:
            raise ConflictError(NO_PHONE_TO_MATCH, code="ACCOUNT_PHONE_MISSING", field="phone")
        if phone_owner is None or phone_owner.id != account.id:
            raise ConflictError(PHONE_MISMATCH, code="ACCOUNT_PHONE_MISMATCH", field="phone")
        return account

    def _check(
        self, owner: User | None, exclude_user_id: int | None, linkable: str, *, taken: str, here: str
    ) -> AccountCheck:
        """AVAILABLE (libre o es la misma persona que se edita), LINKABLE (al dar de alta: persona
        de otra empresa) o TAKEN (administrador, alguien de esta empresa u otra persona al editar)."""
        if owner is None or owner.id == exclude_user_id:
            return AccountCheck("AVAILABLE")
        if self._linkable(owner):
            return AccountCheck("LINKABLE", linkable) if exclude_user_id is None else AccountCheck("TAKEN", taken)
        return AccountCheck("TAKEN", here if owner.role == UserRole.EMPLOYEE else taken)

    def _linkable(self, user: User) -> bool:
        """Empleado de OTRA(S) empresa(s): puede sumarse a esta con la misma cuenta."""
        return user.role == UserRole.EMPLOYEE and all(e.company_id != self.company_id for e in user.employees)
