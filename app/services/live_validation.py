"""Validación en vivo de los formularios: un solo registro de campos para el WebSocket y el HTTP.

Cada campo declara:
- las pantallas que permiten validarlo (las mismas que autorizan sus endpoints): quien no tiene
  la pantalla no puede usar el campo, ni por el canal en tiempo real ni por su respaldo HTTP;
- su verificación: formato y, si el dato es único, disponibilidad.

Todos los correos y teléfonos de la aplicación pasan por aquí mientras se escriben. Agregar un
campo = una entrada en `FIELDS` (y su uso en el frontend).
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from app.core.exceptions import PermissionDeniedError
from app.models import Screen, User
from app.schemas.validators import normalize_phone
from app.services.availability_service import FIELDS as EMPLOYEE_FIELDS
from app.services.availability_service import Availability, AvailabilityService
from app.services.availability_service import Field as EmployeeField
from app.services.catalog_service import get_catalogs
from app.services.company_service import CompanyService

#: (BD, usuario, valor, id excluido al editar, valor relacionado) → resultado.
Checker = Callable[[Session, User, str, int | None, str | None], Availability]


@dataclass(frozen=True)
class LiveField:
    #: Pantallas que permiten validar el campo (cualquiera de ellas).
    screens: tuple[Screen, ...]
    check: Checker


def _employee(field: EmployeeField) -> Checker:
    """Datos del empleado: únicos en su empresa (correo y teléfono, en la plataforma)."""

    def check(db: Session, user: User, value: str, exclude_id: int | None, related: str | None) -> Availability:
        company_id = user.company_id
        if company_id is None:  # la pantalla de empleados solo la tiene quien opera una empresa
            raise PermissionDeniedError("Esta validación corresponde a una empresa", code="COMPANY_REQUIRED")
        service = AvailabilityService(db, company_id)
        return service.check(field, value, exclude_employee_id=exclude_id, related=related)

    return check


def _company(field: str, kind: str) -> Checker:
    """RFC de empresa (único) o correo de una cuenta nueva (único en la plataforma)."""

    def check(db: Session, _user: User, value: str, exclude_id: int | None, _related: str | None) -> Availability:
        result = CompanyService(db).availability("rfc" if kind == "rfc" else "admin_email", value, exclude_id)
        return replace(result, field=field)

    return check


def _format_only(field: str, normalize: Callable[[str], str], empty: str, valid: str) -> Checker:
    """Datos que no son únicos (teléfono de la empresa): solo el formato, con la misma regla que al guardar."""

    def check(_db: Session, _user: User, value: str, _exclude_id: int | None, _related: str | None) -> Availability:
        raw = (value or "").strip()
        if not raw:
            return Availability(field, value, None, False, False, "EMPTY", empty)
        try:
            normalized = normalize(raw)
        except ValueError as exc:
            return Availability(field, value, None, False, False, "INVALID_FORMAT", str(exc))
        return Availability(field, value, normalized, True, True, "VALID", valid)

    return check


FIELDS: dict[str, LiveField] = {
    **{field: LiveField((Screen.COMPANY_EMPLOYEES,), _employee(field)) for field in EMPLOYEE_FIELDS},
    "validator_email": LiveField((Screen.COMPANY_VALIDATORS,), _company("validator_email", "email")),
    "company_rfc": LiveField((Screen.ADMIN_COMPANIES,), _company("company_rfc", "rfc")),
    "company_admin_email": LiveField((Screen.ADMIN_COMPANIES,), _company("company_admin_email", "email")),
    "company_phone": LiveField(
        (Screen.ADMIN_COMPANIES,),
        _format_only("company_phone", normalize_phone, "El teléfono es obligatorio", "Teléfono válido"),
    ),
}


def fields_for(user: User, db: Session | None = None) -> list[str]:
    """Campos que el usuario puede validar (según las pantallas de su rol)."""
    catalogs = get_catalogs(db)
    return [name for name, spec in FIELDS.items() if catalogs.grants(user.role.value, spec.screens)]


def validate_field(
    db: Session, user: User, field: str, value: str, exclude_id: int | None = None, related: str | None = None
) -> Availability:
    """Valida `value` como `field`; 403 si el campo no existe o el rol no tiene su pantalla.

    `related`: otro valor del mismo formulario que la regla necesita (el correo al validar el
    teléfono de un empleado nuevo: si la persona ya trabaja en otra empresa, deben ser de ella).
    """
    spec = FIELDS.get(field)
    if spec is None or not get_catalogs(db).grants(user.role.value, spec.screens):
        raise PermissionDeniedError("No puedes validar este campo", code="FIELD_NOT_ALLOWED")
    return spec.check(db, user, value, exclude_id, related)
