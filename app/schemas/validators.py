"""Validadores reutilizables para los schemas."""

import re
from collections.abc import Callable
from datetime import date
from typing import Annotated

import phonenumbers
from email_validator import EmailNotValidError, validate_email
from pydantic import AfterValidator, Field

from app.core.clock import business_today
from app.i18n import LocalizedValueError, Text
from app.services.catalog_service import get_catalogs

__all__ = ["business_today"]  # fecha del negocio (también la usan las validaciones de fechas)


PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
MIN_EMPLOYEE_AGE = 16
MAX_EMPLOYEE_AGE = 100

_NAME_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ' .-]*$")
_EMPLOYEE_NUMBER_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,29}$")
# RFC de persona física (SAT): 4 letras, fecha aammdd y homoclave de 3 (el último, dígito o "A").
_RFC_RE = re.compile(r"^[A-ZÑ&]{4}(\d{2})(\d{2})(\d{2})[A-Z\d]{2}[\dA]$")
# RFC genéricos (público en general / extranjeros): no identifican a una persona.
_GENERIC_RFCS = frozenset({"XAXX010101000", "XEXX010101000"})
RFC_LENGTH = 13
# CURP (RENAPO): 4 letras, fecha aammdd, sexo (H/M/X), entidad, 3 consonantes, diferenciador de
# siglo (dígito: nacido antes de 2000; letra: 2000 en adelante) y dígito verificador.
_CURP_RE = re.compile(
    r"^[A-Z][AEIOUX][A-Z]{2}(\d{2})(\d{2})(\d{2})[HMX]"
    r"(AS|BC|BS|CC|CL|CM|CS|CH|DF|DG|GT|GR|HG|JC|MC|MN|MS|NT|NL|OC|PL|QT|QR|SP|SL|SR|TC|TS|TL|VZ|YN|ZS|NE)"
    r"[B-DF-HJ-NP-TV-Z]{3}([A-Z\d])(\d)$"
)
_CURP_ALPHABET = "0123456789ABCDEFGHIJKLMN&OPQRSTUVWXYZ"
CURP_LENGTH = 18
NSS_LENGTH = 11
#: Región que se asume cuando un teléfono llega sin lada internacional.
DEFAULT_PHONE_REGION = "MX"
#: "+" y hasta 15 dígitos (UIT-T E.164).
PHONE_E164_MAX_LENGTH = 16


def validate_password_strength(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise LocalizedValueError("PASSWORD_TOO_SHORT", {"count": PASSWORD_MIN_LENGTH})
    if len(value) > PASSWORD_MAX_LENGTH:
        raise LocalizedValueError("PASSWORD_TOO_LONG", {"count": PASSWORD_MAX_LENGTH})
    if not re.search(r"[a-z]", value):
        raise LocalizedValueError("PASSWORD_NEEDS_LOWERCASE")
    if not re.search(r"[A-Z]", value):
        raise LocalizedValueError("PASSWORD_NEEDS_UPPERCASE")
    if not re.search(r"\d", value):
        raise LocalizedValueError("PASSWORD_NEEDS_DIGIT")
    return value


def normalize_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise LocalizedValueError("FIELD_REQUIRED")
    if len(value) > 100:
        raise LocalizedValueError("MAX_CHARACTERS", {"count": 100})
    if not _NAME_RE.match(value):
        raise LocalizedValueError("NAME_INVALID_CHARACTERS")
    return value


def normalize_employee_number(value: str) -> str:
    """Número de empleado en mayúsculas: 1-30 letras, números, guion o guion bajo. Uno vacío no es un número: el dato
    es opcional y lo resuelve antes `optional_document`."""
    value = value.strip().upper()
    if not _EMPLOYEE_NUMBER_RE.match(value):
        raise LocalizedValueError("EMPLOYEE_NUMBER_INVALID")
    return value


#: Lo que se ignora al escribir un documento (RFC, CURP, NSS): espacios y guiones ("PEGJ-900515-AB1").
_DOCUMENT_SEPARATORS = re.compile(r"[\s-]")


def is_blank_document(value: str) -> bool:
    """Dato opcional sin capturar: vacío, o solo espacios y guiones (ninguno de ellos es un número ni un documento
    válido; un «-» se escribe a veces para decir «no tiene»)."""
    return not _DOCUMENT_SEPARATORS.sub("", value)


def optional_document(value: str | None, normalize: Callable[[str], str]) -> str | None:
    """RFC, CURP y NSS del empleado son OPCIONALES (decisión del dueño del producto: la plataforma se abre a otros
    países, donde no existen; el identificador fiscal de la empresa también, `app/schemas/tax_ids.py`) y el número de
    empleado también (decisión del dueño, migración 0076). Sin capturar es `None` (NULL en la BD, nunca ""; los índices
    únicos admiten varios NULL); con valor, `normalize` aplica todas sus reglas (formato, longitud y dígito
    verificador)."""
    if value is None or is_blank_document(value):
        return None
    return normalize(value)


def _rfc_date_is_valid(yy: int, mm: int, dd: int) -> bool:
    """La fecha del RFC no indica el siglo: es válida si existe en 19aa o en 20aa (29 de febrero)."""
    for century in (1900, 2000):
        try:
            date(century + yy, mm, dd)
        except ValueError:
            continue
        return True
    return False


def normalize_rfc(value: str) -> str:
    """RFC en mayúsculas, sin espacios ni guiones, validado contra el formato de persona física. Uno vacío no es un
    RFC: si el dato es opcional lo resuelve antes `optional_document`."""
    value = _DOCUMENT_SEPARATORS.sub("", value).upper()
    if value in _GENERIC_RFCS:
        raise LocalizedValueError("RFC_GENERIC")
    if len(value) != RFC_LENGTH:
        raise LocalizedValueError("RFC_LENGTH", {"length": RFC_LENGTH, "count": len(value)})
    match = _RFC_RE.match(value)
    if not match:
        raise LocalizedValueError("RFC_FORMAT")
    if not _rfc_date_is_valid(*(int(part) for part in match.groups())):
        raise LocalizedValueError("RFC_DATE_INVALID")
    return value


def rfc_matches_birth_date(rfc: str, birth_date: date) -> bool:
    """En el RFC de persona física, los caracteres 5 a 10 son la fecha de nacimiento (aammdd)."""
    return rfc[4:10] == birth_date.strftime("%y%m%d")


# RFC de persona moral (empresa): 3 letras, fecha de constitución aammdd y homoclave.
_COMPANY_RFC_RE = re.compile(r"^[A-ZÑ&]{3}(\d{2})(\d{2})(\d{2})[A-Z\d]{2}[\dA]$")


def normalize_email(value: str) -> str:
    """Correo normalizado como se guarda (minúsculas, dominio en forma canónica)."""
    try:
        return validate_email(value.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise LocalizedValueError("EMAIL_INVALID") from exc


def normalize_company_rfc(value: str) -> str:
    """RFC de la empresa (su identificador fiscal de tipo `MX_RFC`, `app/schemas/tax_ids.py`): persona moral (12) o
    persona física con actividad empresarial (13). Uno vacío no es un RFC: el identificador es opcional y lo resuelve
    antes quien llama."""
    value = _DOCUMENT_SEPARATORS.sub("", value).upper()
    if len(value) == RFC_LENGTH:
        # 13 caracteres: persona física (también rechaza los RFC genéricos, que tienen 13).
        return normalize_rfc(value)
    match = _COMPANY_RFC_RE.match(value)
    if not match:
        raise LocalizedValueError("COMPANY_RFC_LENGTH")
    if not _rfc_date_is_valid(*(int(part) for part in match.groups())):
        raise LocalizedValueError("RFC_DATE_INVALID")
    return value


def normalize_company_name(value: str, required: str = "NAME_REQUIRED") -> str:
    """Nombre de la empresa; `required` es la llave del mensaje cuando falta (cada campo lo dice con su nombre)."""
    value = " ".join(value.split())
    if len(value) < 2:
        raise LocalizedValueError(required)
    if len(value) > 200:
        raise LocalizedValueError("MAX_CHARACTERS", {"count": 200})
    return value


def curp_check_digit(first17: str) -> str:
    """Dígito verificador de la CURP (algoritmo de RENAPO)."""
    total = sum(_CURP_ALPHABET.index(char) * (18 - i) for i, char in enumerate(first17))
    return str((10 - total % 10) % 10)


def normalize_curp(value: str) -> str:
    """CURP en mayúsculas, sin espacios ni guiones, con formato de RENAPO y su dígito verificador (vacía no es una
    CURP: ver `optional_document`)."""
    value = _DOCUMENT_SEPARATORS.sub("", value).upper()
    if len(value) != CURP_LENGTH:
        raise LocalizedValueError("CURP_LENGTH", {"length": CURP_LENGTH, "count": len(value)})
    match = _CURP_RE.match(value)
    if not match:
        raise LocalizedValueError("CURP_FORMAT")
    yy, mm, dd = (int(part) for part in match.groups()[:3])
    if not _rfc_date_is_valid(yy, mm, dd):
        raise LocalizedValueError("CURP_DATE_INVALID")
    if curp_check_digit(value[:17]) != value[17]:
        raise LocalizedValueError("CURP_CHECK_DIGIT")
    return value


def _document_date(yymmdd: str, birth_date: date) -> date | str:
    """Fecha aammdd de un RFC o una CURP con el siglo de la fecha capturada (se escribe como fecha del idioma). Si
    con ese siglo no existe (29 de febrero), tal cual dd/mm/aaaa."""
    year, month, day = int(f"{str(birth_date.year)[:2]}{yymmdd[:2]}"), int(yymmdd[2:4]), int(yymmdd[4:6])
    try:
        return date(year, month, day)
    except ValueError:
        return f"{yymmdd[4:6]}/{yymmdd[2:4]}/{year}"


def rfc_birth_date_error(rfc: str, birth_date: date) -> Text | None:
    """Qué fecha indica el RFC y cuál se capturó, para corregir la que esté mal."""
    if rfc_matches_birth_date(rfc, birth_date):
        return None
    return Text("RFC_BIRTH_DATE_MISMATCH", {"document": _document_date(rfc[4:10], birth_date), "birth": birth_date})


def curp_birth_date_error(curp: str, birth_date: date) -> Text | None:
    """Fecha (aammdd) y siglo (carácter 17) de la CURP contra la fecha de nacimiento capturada."""
    if curp[4:10] != birth_date.strftime("%y%m%d"):
        return Text(
            "CURP_BIRTH_DATE_MISMATCH", {"document": _document_date(curp[4:10], birth_date), "birth": birth_date}
        )
    if curp[16].isdigit() != (birth_date.year < 2000):
        return Text("CURP_CENTURY_MISMATCH")
    return None


def luhn_valid(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        number = int(char)
        if position % 2 == 1:
            number = number * 2 - 9 if number > 4 else number * 2
        total += number
    return total % 10 == 0


def normalize_nss(value: str) -> str:
    """Número de Seguridad Social (IMSS): 11 dígitos; el último es verificador (algoritmo Luhn). Vacío no es un NSS:
    ver `optional_document`."""
    value = _DOCUMENT_SEPARATORS.sub("", value)
    if not value.isdigit() or len(value) != NSS_LENGTH:
        raise LocalizedValueError("NSS_LENGTH", {"count": NSS_LENGTH})
    if not luhn_valid(value):
        raise LocalizedValueError("NSS_CHECK_DIGIT")
    return value


def normalize_phone(value: str) -> str:
    """Teléfono internacional en formato E.164 (`+<lada><número>`), validado con libphonenumber.

    Acepta espacios, guiones y paréntesis. Sin lada se asume México (+52); el prefijo de celular
    anterior a 2019 (`+52 1 …`) se convierte al formato vigente.
    """
    digits = re.sub(r"\D", "", value)
    if not digits:
        raise LocalizedValueError("PHONE_REQUIRED")
    raw = value.strip()
    legacy_mx_mobile = re.fullmatch(r"521(\d{10})", digits)
    if legacy_mx_mobile and (raw.startswith("+") or len(digits) == len("521") + 10):
        raw = f"+52{legacy_mx_mobile.group(1)}"
    try:
        number = phonenumbers.parse(raw, DEFAULT_PHONE_REGION)
    except phonenumbers.NumberParseException:
        raise LocalizedValueError("PHONE_INVALID") from None
    national = str(number.national_number)
    if not phonenumbers.is_valid_number(number) or len(set(national)) == 1:
        raise LocalizedValueError("PHONE_INVALID_FOR_CODE", {"code": number.country_code})
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def ensure_active_country(phone: str) -> str:
    """La lada debe ser de un país activo del catálogo (catalog.countries)."""
    region = phonenumbers.region_code_for_number(phonenumbers.parse(phone))
    if region is None or not get_catalogs().is_active("countries", region):
        raise LocalizedValueError("PHONE_COUNTRY_UNAVAILABLE")
    return phone


#: Teléfono en los esquemas de entrada (empleados y empresas): se guarda en E.164.
PhoneNumber = Annotated[
    str,
    Field(max_length=25, description="Teléfono con lada internacional (E.164)", examples=["+526621234567"]),
    AfterValidator(normalize_phone),
    AfterValidator(ensure_active_country),
]


def validate_birth_date(value: date) -> date:
    today = business_today()
    if value >= today:
        raise LocalizedValueError("BIRTH_DATE_NOT_PAST")
    age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
    if age < MIN_EMPLOYEE_AGE:
        raise LocalizedValueError("EMPLOYEE_TOO_YOUNG", {"count": MIN_EMPLOYEE_AGE})
    if age > MAX_EMPLOYEE_AGE:
        raise LocalizedValueError("BIRTH_DATE_INVALID")
    return value
