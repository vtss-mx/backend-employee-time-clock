"""Validación en vivo de los formularios: un solo registro de campos para el WebSocket y el HTTP.

Cada campo declara:
- las pantallas que permiten validarlo (las mismas que autorizan sus endpoints): quien no tiene
  la pantalla no puede usar el campo, ni por el canal en tiempo real ni por su respaldo HTTP;
- su verificación: formato y, si el dato es único, disponibilidad.

Todos los correos y teléfonos de la aplicación pasan por aquí mientras se escriben. Agregar un
campo = una entrada en `FIELDS` (y su uso en el frontend).
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.exceptions import PermissionDeniedError
from app.i18n import Text, error_text
from app.models import Screen, User
from app.schemas.validators import normalize_phone
from app.services.availability_service import FIELDS as EMPLOYEE_FIELDS
from app.services.availability_service import Availability, AvailabilityService
from app.services.availability_service import Field as EmployeeField
from app.services.catalog_service import get_catalogs
from app.services.company_service import CompanyService
from app.services.department_service import DepartmentService
from app.services.validator_service import validators_module

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
            raise PermissionDeniedError(code="COMPANY_REQUIRED", key="VALIDATION_COMPANY_REQUIRED")
        service = AvailabilityService(db, company_id)
        return service.check(field, value, exclude_employee_id=exclude_id, related=related)

    return check


def _account_email(field: str) -> Checker:
    """Correo de una cuenta nueva (administrador de empresa o validador): único en la plataforma."""

    def check(db: Session, _user: User, value: str, _exclude_id: int | None, _related: str | None) -> Availability:
        return CompanyService(db).email_availability(field, value)

    return check


def _company_tax_id(field: str, fixed: str | None = None) -> Checker:
    """Identificador fiscal de una empresa: formato de su tipo y único por país, tipo y número (la empresa que se
    edita, `exclude_id`, no cuenta). `related` = «país:tipo» del formulario («US:US_EIN»; lo que falte, el de omisión:
    `tax_id_defaults`). `fixed` lo fija: el campo anterior `company_rfc` (obsoleto, solo RFC de México) se queda para
    la aplicación web anterior en marcha durante un despliegue y se quita junto con la columna `rfc`."""

    def check(db: Session, _user: User, value: str, exclude_id: int | None, related: str | None) -> Availability:
        country, _, type_code = (fixed or related or "").partition(":")
        return CompanyService(db).tax_id_availability(field, value, country or None, type_code or None, exclude_id)

    return check


def _validator_email(db: Session, user: User, value: str, exclude_id: int | None, related: str | None) -> Availability:
    """Correo de un validador nuevo: solo con el módulo de validadores (403 VALIDATORS_DISABLED, igual que sus APIs)."""
    validators_module(user)
    return _account_email("validator_email")(db, user, value, exclude_id, related)


def _department_name(db: Session, user: User, value: str, exclude_id: int | None, _related: str | None) -> Availability:
    """Nombre de departamento: único en la empresa (sin distinguir mayúsculas)."""
    if user.company_id is None:
        raise PermissionDeniedError(code="COMPANY_REQUIRED", key="VALIDATION_COMPANY_REQUIRED")
    return DepartmentService(db, user.company_id).name_availability(value, exclude_id)


def _format_only(field: str, normalize: Callable[[str], str], empty: str, valid: str) -> Checker:
    """Datos que no son únicos (teléfono de la empresa): solo el formato, con la misma regla que al guardar. `empty` y
    `valid` son las llaves de sus mensajes."""

    def check(_db: Session, _user: User, value: str, _exclude_id: int | None, _related: str | None) -> Availability:
        raw = (value or "").strip()
        if not raw:
            return Availability(field, value, None, False, False, "EMPTY", Text(empty))
        try:
            normalized = normalize(raw)
        except ValueError as exc:
            return Availability(field, value, None, False, False, "INVALID_FORMAT", error_text(exc))
        return Availability(field, value, normalized, True, True, "VALID", Text(valid))

    return check


FIELDS: dict[str, LiveField] = {
    **{field: LiveField((Screen.COMPANY_EMPLOYEES,), _employee(field)) for field in EMPLOYEE_FIELDS},
    "validator_email": LiveField((Screen.COMPANY_VALIDATORS,), _validator_email),
    "department_name": LiveField((Screen.COMPANY_DEPARTMENTS,), _department_name),
    "company_tax_id": LiveField((Screen.ADMIN_COMPANIES,), _company_tax_id("company_tax_id")),
    "company_rfc": LiveField((Screen.ADMIN_COMPANIES,), _company_tax_id("company_rfc", "MX:MX_RFC")),
    "company_admin_email": LiveField((Screen.ADMIN_COMPANIES,), _account_email("company_admin_email")),
    "company_phone": LiveField(
        (Screen.ADMIN_COMPANIES,),
        _format_only("company_phone", normalize_phone, "PHONE_REQUIRED", "PHONE_VALID"),
    ),
}


def fields_for(user: User) -> list[str]:
    """Campos que el usuario puede validar (según las pantallas de su rol)."""
    catalogs = get_catalogs()
    return [name for name, spec in FIELDS.items() if catalogs.grants(user.role.value, spec.screens)]


def validate_field(
    db: Session, user: User, field: str, value: str, exclude_id: int | None = None, related: str | None = None
) -> Availability:
    """Valida `value` como `field`; 403 si el campo no existe o el rol no tiene su pantalla.

    `related`: otro valor del mismo formulario que la regla necesita (el correo al validar el
    teléfono de un empleado nuevo: si la persona ya trabaja en otra empresa, deben ser de ella; el
    «país:tipo» al validar el identificador fiscal de una empresa).
    """
    spec = FIELDS.get(field)
    if spec is None or not get_catalogs().grants(user.role.value, spec.screens):
        raise PermissionDeniedError(code="FIELD_NOT_ALLOWED")
    return spec.check(db, user, value, exclude_id, related)
