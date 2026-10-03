"""Disponibilidad de datos únicos del empleado (número, RFC, CURP, NSS, correo y teléfono) en tiempo real.

Lo usan el canal WebSocket de validación, el endpoint HTTP de respaldo y el alta/edición de
empleados (la regla y los mensajes viven en un solo lugar).
"""

from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

from sqlalchemy.orm import Session

from app.repositories.employee_repository import EmployeeRepository, UniqueDocument
from app.schemas.validators import (
    normalize_curp,
    normalize_email,
    normalize_employee_number,
    normalize_nss,
    normalize_phone,
    normalize_rfc,
)
from app.services.people_service import AccountCheck, PeopleService

Field = Literal["employee_number", "rfc", "curp", "nss", "email", "phone"]
FIELDS: tuple[Field, ...] = ("employee_number", "rfc", "curp", "nss", "email", "phone")

NUMBER_TAKEN = "El número de empleado ya está registrado"
RFC_TAKEN = "El RFC ya está registrado en otro empleado"
CURP_TAKEN = "La CURP ya está registrada en otro empleado"
NSS_TAKEN = "El NSS ya está registrado en otro empleado"
_AVAILABLE = {
    "employee_number": "Número de empleado disponible",
    "rfc": "RFC disponible",
    "curp": "CURP disponible",
    "nss": "NSS disponible",
    "email": "Correo disponible",
    "phone": "Teléfono disponible",
}
_TAKEN = {"employee_number": NUMBER_TAKEN, "rfc": RFC_TAKEN, "curp": CURP_TAKEN, "nss": NSS_TAKEN}
_EMPTY = {
    "employee_number": "El número de empleado es obligatorio",
    "rfc": "El RFC es obligatorio",
    "curp": "La CURP es obligatoria",
    "nss": "El NSS es obligatorio",
    "email": "El correo es obligatorio",
    "phone": "El teléfono es obligatorio",
}


@dataclass(frozen=True)
class Availability:
    field: str
    value: str
    #: Valor tal como se guardaría (mayúsculas, minúsculas, sin espacios).
    normalized: str | None
    valid: bool
    available: bool
    #: AVAILABLE | LINKABLE (persona de otra empresa: se vincula) | TAKEN | INVALID_FORMAT | EMPTY
    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AvailabilityService:
    """Únicos dentro de la empresa (número, RFC, CURP, NSS); el correo es único en la plataforma."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.employees = EmployeeRepository(db, company_id)
        self.people = PeopleService(db, company_id)

    def check(
        self, field: Field, value: str, *, exclude_employee_id: int | None = None, related: str | None = None
    ) -> Availability:
        """`related`: al validar el teléfono en un alta, el correo escrito (deben ser de la misma
        persona si ya trabaja en otra empresa)."""
        raw = (value or "").strip()
        if not raw:
            return Availability(field, value, None, False, False, "EMPTY", _EMPTY[field])
        try:
            normalized = self._normalize(field, raw)
        except ValueError as exc:
            return Availability(field, value, None, False, False, "INVALID_FORMAT", str(exc))
        if field in ("email", "phone"):
            check = self._account_check(field, normalized, exclude_employee_id, related)
            message = check.message or _AVAILABLE[field]
            usable = check.match in ("AVAILABLE", "LINKABLE")
            return Availability(field, value, normalized, True, usable, check.match, message)
        taken = self._exists(field, normalized, exclude_employee_id)
        code = "TAKEN" if taken else "AVAILABLE"
        message = _TAKEN[field] if taken else _AVAILABLE[field]
        return Availability(field, value, normalized, True, not taken, code, message)

    @staticmethod
    def _normalize(field: Field, value: str) -> str:
        if field == "employee_number":
            return normalize_employee_number(value)
        if field == "rfc":
            return normalize_rfc(value)
        if field == "curp":
            return normalize_curp(value)
        if field == "nss":
            return normalize_nss(value)
        if field == "phone":
            return normalize_phone(value)
        return normalize_email(value)

    def _exists(self, field: Field, value: str, exclude_employee_id: int | None) -> bool:
        if field == "employee_number":
            return self.employees.number_exists(value, exclude_id=exclude_employee_id)
        return self.employees.unique_exists(cast(UniqueDocument, field), value, exclude_id=exclude_employee_id)

    def _account_check(
        self, field: Field, value: str, exclude_employee_id: int | None, related: str | None = None
    ) -> AccountCheck:
        """Correo y teléfono son de la persona: únicos en la plataforma (o vinculables)."""
        paired = self._phone_with_email(value, related) if field == "phone" and exclude_employee_id is None else None
        if paired is not None:
            return paired
        exclude = self.employees.get_by_id(exclude_employee_id) if exclude_employee_id else None
        exclude_user_id = exclude.user_id if exclude else None
        if field == "email":
            return self.people.check_email(value, exclude_user_id=exclude_user_id)
        return self.people.check_phone(value, exclude_user_id=exclude_user_id)

    def _phone_with_email(self, phone: str, email: str | None) -> AccountCheck | None:
        """Con un correo válido escrito, el teléfono se valida junto con él (persona en varias empresas)."""
        try:
            normalized_email = normalize_email(email or "")
        except ValueError:
            return None
        return self.people.check_phone_for(phone, normalized_email)
