"""Validadores reutilizables para los schemas."""

import re
from datetime import date
from typing import Annotated

import phonenumbers
from email_validator import EmailNotValidError, validate_email
from pydantic import AfterValidator, Field

from app.core.clock import business_today
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
        raise ValueError(f"La contraseña debe tener al menos {PASSWORD_MIN_LENGTH} caracteres")
    if len(value) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"La contraseña no debe exceder {PASSWORD_MAX_LENGTH} caracteres")
    if not re.search(r"[a-z]", value):
        raise ValueError("La contraseña debe incluir al menos una letra minúscula")
    if not re.search(r"[A-Z]", value):
        raise ValueError("La contraseña debe incluir al menos una letra mayúscula")
    if not re.search(r"\d", value):
        raise ValueError("La contraseña debe incluir al menos un número")
    return value


def normalize_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Este campo es obligatorio")
    if len(value) > 100:
        raise ValueError("Máximo 100 caracteres")
    if not _NAME_RE.match(value):
        raise ValueError("Solo se permiten letras, espacios, apóstrofes, puntos y guiones")
    return value


def normalize_employee_number(value: str) -> str:
    value = value.strip().upper()
    if not _EMPLOYEE_NUMBER_RE.match(value):
        raise ValueError("El número de empleado debe tener 1-30 caracteres: letras, números, guion o guion bajo")
    return value


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
    """RFC en mayúsculas, sin espacios ni guiones, validado contra el formato de persona física."""
    value = re.sub(r"[\s-]", "", value).upper()
    if not value:
        raise ValueError("El RFC es obligatorio")
    if value in _GENERIC_RFCS:
        raise ValueError("Captura el RFC personal del empleado; el RFC genérico no es válido")
    if len(value) != RFC_LENGTH:
        raise ValueError(f"El RFC de una persona física tiene {RFC_LENGTH} caracteres; se escribieron {len(value)}")
    match = _RFC_RE.match(value)
    if not match:
        raise ValueError("El RFC no tiene un formato válido (p. ej. PEGJ900515AB1)")
    if not _rfc_date_is_valid(*(int(part) for part in match.groups())):
        raise ValueError("La fecha del RFC (aammdd) no es válida")
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
        raise ValueError("Correo electrónico inválido") from exc


def normalize_company_rfc(value: str) -> str:
    """RFC de la empresa: persona moral (12) o persona física con actividad empresarial (13)."""
    value = re.sub(r"[\s-]", "", value).upper()
    if not value:
        raise ValueError("El RFC es obligatorio")
    if len(value) == RFC_LENGTH:
        return normalize_rfc(value)
    if value in _GENERIC_RFCS:
        raise ValueError("Captura el RFC de la empresa; el RFC genérico no es válido")
    match = _COMPANY_RFC_RE.match(value)
    if not match:
        raise ValueError("El RFC debe tener 12 caracteres (persona moral) o 13 (persona física)")
    if not _rfc_date_is_valid(*(int(part) for part in match.groups())):
        raise ValueError("La fecha del RFC (aammdd) no es válida")
    return value


def normalize_company_name(value: str, field: str = "El nombre") -> str:
    value = " ".join(value.split())
    if len(value) < 2:
        raise ValueError(f"{field} es obligatorio")
    if len(value) > 200:
        raise ValueError("Máximo 200 caracteres")
    return value


def curp_check_digit(first17: str) -> str:
    """Dígito verificador de la CURP (algoritmo de RENAPO)."""
    total = sum(_CURP_ALPHABET.index(char) * (18 - i) for i, char in enumerate(first17))
    return str((10 - total % 10) % 10)


def normalize_curp(value: str) -> str:
    value = re.sub(r"[\s-]", "", value).upper()
    if not value:
        raise ValueError("La CURP es obligatoria")
    if len(value) != CURP_LENGTH:
        raise ValueError(f"La CURP tiene {CURP_LENGTH} caracteres; se escribieron {len(value)}")
    match = _CURP_RE.match(value)
    if not match:
        raise ValueError("La CURP no tiene un formato válido (p. ej. HEGG560427MVZRRL04)")
    yy, mm, dd = (int(part) for part in match.groups()[:3])
    if not _rfc_date_is_valid(yy, mm, dd):
        raise ValueError("La fecha de la CURP (aammdd) no es válida")
    if curp_check_digit(value[:17]) != value[17]:
        raise ValueError("La CURP no es válida: el dígito verificador no corresponde")
    return value


def curp_matches_birth_date(curp: str, birth_date: date) -> bool:
    """Fecha aammdd y siglo: el carácter 17 es dígito si nació antes de 2000 y letra después."""
    return curp_birth_date_error(curp, birth_date) is None


def _document_date(yymmdd: str, birth_date: date) -> str:
    """Fecha aammdd de un RFC o una CURP como dd/mm/aaaa (con el siglo de la fecha capturada)."""
    return f"{yymmdd[4:6]}/{yymmdd[2:4]}/{str(birth_date.year)[:2]}{yymmdd[:2]}"


def rfc_birth_date_error(rfc: str, birth_date: date) -> str | None:
    """Qué fecha indica el RFC y cuál se capturó, para corregir la que esté mal."""
    if rfc_matches_birth_date(rfc, birth_date):
        return None
    return (
        f"El RFC indica nacimiento el {_document_date(rfc[4:10], birth_date)}, "
        f"pero la fecha de nacimiento es {birth_date:%d/%m/%Y}"
    )


def curp_birth_date_error(curp: str, birth_date: date) -> str | None:
    """Fecha (aammdd) y siglo (carácter 17) de la CURP contra la fecha de nacimiento capturada."""
    if curp[4:10] != birth_date.strftime("%y%m%d"):
        return (
            f"La CURP indica nacimiento el {_document_date(curp[4:10], birth_date)}, "
            f"pero la fecha de nacimiento es {birth_date:%d/%m/%Y}"
        )
    if curp[16].isdigit() != (birth_date.year < 2000):
        return (
            "La CURP no corresponde al siglo de la fecha de nacimiento: su carácter 17 es un número "
            "para quienes nacieron antes de 2000 y una letra a partir de 2000"
        )
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
    """Número de Seguridad Social (IMSS): 11 dígitos; el último es verificador (algoritmo Luhn)."""
    value = re.sub(r"[\s-]", "", value)
    if not value:
        raise ValueError("El NSS es obligatorio")
    if not value.isdigit() or len(value) != NSS_LENGTH:
        raise ValueError(f"El NSS tiene {NSS_LENGTH} dígitos")
    if not luhn_valid(value):
        raise ValueError("El NSS no es válido: el dígito verificador no corresponde")
    return value


def normalize_phone(value: str) -> str:
    """Teléfono internacional en formato E.164 (`+<lada><número>`), validado con libphonenumber.

    Acepta espacios, guiones y paréntesis. Sin lada se asume México (+52); el prefijo de celular
    anterior a 2019 (`+52 1 …`) se convierte al formato vigente.
    """
    digits = re.sub(r"\D", "", value)
    if not digits:
        raise ValueError("El teléfono es obligatorio")
    raw = value.strip()
    legacy_mx_mobile = re.fullmatch(r"521(\d{10})", digits)
    if legacy_mx_mobile and (raw.startswith("+") or len(digits) == len("521") + 10):
        raw = f"+52{legacy_mx_mobile.group(1)}"
    try:
        number = phonenumbers.parse(raw, DEFAULT_PHONE_REGION)
    except phonenumbers.NumberParseException:
        raise ValueError("El teléfono no es válido") from None
    national = str(number.national_number)
    if not phonenumbers.is_valid_number(number) or len(set(national)) == 1:
        raise ValueError(f"El teléfono no es válido para la lada +{number.country_code}")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def ensure_active_country(phone: str) -> str:
    """La lada debe ser de un país activo del catálogo (catalog.countries)."""
    region = phonenumbers.region_code_for_number(phonenumbers.parse(phone))
    if region is None or not get_catalogs().is_active("countries", region):
        raise ValueError("Los teléfonos de ese país no están disponibles")
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
        raise ValueError("La fecha de nacimiento debe ser anterior a hoy")
    age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
    if age < MIN_EMPLOYEE_AGE:
        raise ValueError(f"El empleado debe tener al menos {MIN_EMPLOYEE_AGE} años")
    if age > MAX_EMPLOYEE_AGE:
        raise ValueError("La fecha de nacimiento no es válida")
    return value
