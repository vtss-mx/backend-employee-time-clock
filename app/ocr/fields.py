"""Extracción de campos del texto que reconoce Tesseract (reglas puras: sin BD, sin red, sin estado).

Mejor esfuerzo (decisión del dueño del producto, 2026-10-07): se extrae lo que se pueda y la EMPRESA confirma o
corrige; una lectura imperfecta NUNCA bloquea. La fiabilidad depende del tipo de documento (detalle en
`docs/rd/documentos-ocr-2026-10-07.md`):

- Pasaporte e identificación con MRZ (ICAO 9303): la zona de lectura mecánica (`mrz.py`) da nombre, fecha de
  nacimiento, número, nacionalidad, sexo y vigencia con dígitos verificadores. Es la fuente MÁS fiable y tiene
  prioridad sobre el texto suelto.
- INE (credencial para votar): además de la MRZ del reverso, la CURP y la clave de elector del anverso por su formato
  (expresiones regulares). De la CURP se derivan la fecha de nacimiento y el sexo si la MRZ no los trajo.
- Licencia y «otro documento oficial»: OCR general; se intentan la fecha de nacimiento y un número de documento del
  texto, sin garantía.
- Comprobante de domicilio: el código postal (5 dígitos) y un fragmento de domicilio, de mejor esfuerzo.

Todos los campos quedan como TEXTO (los edita la empresa). Las claves estables son las columnas del expediente:
`full_name`, `document_number`, `birth_date` (ISO), `expiry_date` (ISO), `nationality`, `sex`, `curp`,
`voter_key` (clave de elector), `postal_code`, `address`.
"""

import re
from datetime import date

from app.core.clock import business_today
from app.ocr.mrz import MrzResult, parse_mrz

#: Tipos de documento del empleado (catálogo `catalog.employee_document_types`; los usa la lógica de OCR). No es un
#: `StrEnum` del catálogo (como `company_document_types`): son constantes de este módulo y del servicio.
PASSPORT = "PASSPORT"
NATIONAL_ID = "NATIONAL_ID"  # INE / credencial para votar y equivalentes de otros países
DRIVER_LICENSE = "DRIVER_LICENSE"
OTHER_OFFICIAL_ID = "OTHER_OFFICIAL_ID"
PROOF_OF_ADDRESS = "PROOF_OF_ADDRESS"
#: Tipos que son una identificación oficial (uno de estos cubre el requisito de identificación en el onboarding).
OFFICIAL_ID_TYPES = frozenset({PASSPORT, NATIONAL_ID, DRIVER_LICENSE, OTHER_OFFICIAL_ID})
#: Tipos que leen la MRZ de forma fiable.
MRZ_TYPES = frozenset({PASSPORT, NATIONAL_ID})

#: CURP mexicana: 4 letras + 6 dígitos (fecha) + H/M (sexo) + 5 letras (entidad y consonantes) + 1 alfanumérico + 1
#: dígito. Se busca como palabra completa.
_CURP = re.compile(r"\b([A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d)\b")
#: Clave de elector del INE: 18 caracteres (6 letras + 8 dígitos + 4 alfanuméricos), en una sola palabra.
_VOTER_KEY = re.compile(r"\b([A-Z]{6}\d{8}[A-Z0-9]{3}\d)\b")
#: Fecha en los formatos comunes de un documento (DD/MM/AAAA, DD-MM-AAAA, AAAA-MM-DD, con `/`, `-`, `.` o espacio).
_DATE = re.compile(r"\b(\d{1,2})[\s./-](\d{1,2})[\s./-](\d{2,4})\b|\b(\d{4})[\s./-](\d{1,2})[\s./-](\d{1,2})\b")
#: Código postal de 5 dígitos precedido por «C.P.» / «CP» / «Código Postal» (evita confundirlo con otros números).
_POSTAL = re.compile(r"(?:C\.?\s*P\.?|C[OÓ]DIGO\s+POSTAL)\s*:?\s*(\d{5})\b", re.IGNORECASE)
#: Entidad y consonantes de la CURP para derivar sexo y fecha de nacimiento si la MRZ no los trajo.
_CURP_SEX = {"H": "M", "M": "F"}


def _parse_date(match: re.Match[str]) -> date | None:
    """Una coincidencia de `_DATE` como fecha (día/mes/año o año/mes/día); None si no es una fecha válida."""
    if match.group(1):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    else:
        year, month, day = int(match.group(4)), int(match.group(5)), int(match.group(6))
    if year < 100:
        year += 2000 if year <= business_today().year % 100 else 1900
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _curp_birth(curp: str) -> date | None:
    """Fecha de nacimiento codificada en la CURP (posiciones 5-10, `AAMMDD`). El siglo lo da la homoclave (17.º
    carácter): un dígito = nacido en el siglo XX (1900+), una letra = en el siglo XXI (2000+)."""
    digits = curp[4:10]
    year, month, day = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
    century = 1900 if curp[16].isdigit() else 2000
    try:
        return date(century + year, month, day)
    except ValueError:
        return None


def extract_fields(document_type: str, text: str) -> tuple[dict[str, str], MrzResult | None]:
    """Los campos legibles del documento y la MRZ (si la tiene). La MRZ tiene prioridad; lo demás solo rellena lo que
    falte (nunca pisa un dato verificado)."""
    fields: dict[str, str] = {}
    mrz = parse_mrz(text) if document_type in MRZ_TYPES else None
    if mrz is not None:
        fields.update(mrz.fields)
    if document_type == NATIONAL_ID:
        _ine_fields(text, fields)
    elif document_type == PROOF_OF_ADDRESS:
        _address_fields(text, fields)
    elif document_type in (DRIVER_LICENSE, OTHER_OFFICIAL_ID):
        _general_fields(text, fields)
    return fields, mrz


def _ine_fields(text: str, fields: dict[str, str]) -> None:
    """CURP, clave de elector y, de la CURP, la fecha de nacimiento y el sexo (si faltan)."""
    upper = text.upper()
    curp_match = _CURP.search(upper)
    if curp_match:
        curp = curp_match.group(1)
        fields.setdefault("curp", curp)
        fields.setdefault("document_number", curp)
        fields.setdefault("sex", _CURP_SEX.get(curp[10], curp[10]))
        birth = _curp_birth(curp)
        if birth is not None:
            fields.setdefault("birth_date", birth.isoformat())
    voter = _VOTER_KEY.search(upper)
    if voter:
        fields.setdefault("voter_key", voter.group(1))


def _address_fields(text: str, fields: dict[str, str]) -> None:
    """Código postal y un fragmento de domicilio (las líneas no vacías más largas), de mejor esfuerzo."""
    postal = _POSTAL.search(text)
    if postal:
        fields["postal_code"] = postal.group(1)
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) >= 8]
    if lines:
        fields.setdefault("address", " / ".join(lines[:3])[:300])


def _general_fields(text: str, fields: dict[str, str]) -> None:
    """Licencia u «otro documento»: la primera fecha creíble como posible nacimiento (la empresa lo confirma)."""
    for match in _DATE.finditer(text):
        parsed = _parse_date(match)
        if parsed is not None and parsed < business_today():
            fields.setdefault("birth_date", parsed.isoformat())
            break
