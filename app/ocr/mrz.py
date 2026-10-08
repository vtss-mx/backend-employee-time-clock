"""Zona de lectura mecánica (MRZ, ICAO 9303) de pasaportes y credenciales: lectura FIABLE, sin red ni dependencias.

Decisión del dueño del producto (2026-10-07): el pasaporte y la INE/identificación se leen «por su zona de lectura de
forma fiable». La MRZ es una zona de texto con tipografía OCR-B y un formato FIJO (ICAO 9303) con dígitos verificadores:
aunque el reconocimiento de la foto tenga ruido, los campos y sus dígitos permiten extraer el nombre, la fecha de
nacimiento, el número de documento, la nacionalidad, el sexo y la vigencia con alta confianza.

Se analiza con un parser PROPIO (expresiones regulares sobre el texto de Tesseract) en vez de `PassportEye`/`mrz`:
- Regla 13 de la raíz: nada sale del servidor y no se agrega una dependencia nueva (ni descargas).
- `PassportEye` arrastra `scikit-image`, `imutils` y su propio detector de la zona, y descarga datos: no aporta sobre
  leer la MRZ que Tesseract ya reconoció.
El parser es de MEJOR ESFUERZO (decisión del dueño): si un dígito verificador no cuadra, igual se devuelven los campos
(marcando `verified=False`); nunca se rechaza por una lectura imperfecta. La empresa confirma o corrige.

Formatos (ICAO 9303):
- TD3 (pasaporte): 2 renglones de 44 caracteres.
- TD1 (credenciales e identificaciones; la INE reciente la lleva al reverso): 3 renglones de 30 caracteres.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

from app.core.clock import business_today

#: Valor de cada carácter para el dígito verificador (0-9, A-Z = 10-35, relleno `<` = 0).
_VALUES = {str(d): d for d in range(10)} | {chr(ord("A") + i): 10 + i for i in range(26)} | {"<": 0}
#: Pesos cíclicos del algoritmo de ICAO 9303.
_WEIGHTS = (7, 3, 1)
#: Una línea de MRZ solo tiene mayúsculas A-Z, dígitos y el relleno `<`. Tesseract confunde `O`/`0`, `I`/`1`, etc.:
#: la limpieza previa lo deja en ese alfabeto antes de medir.
_ALLOWED = re.compile(r"[^A-Z0-9<]")


@dataclass(frozen=True)
class MrzResult:
    """Lo que la MRZ aporta (mejor esfuerzo). Las fechas quedan como texto ISO si se pudieron armar; el número de
    documento sin el relleno. `verified` es True solo si TODOS los dígitos verificadores presentes cuadraron."""

    document_type: str  # "PASSPORT" (TD3) o "ID_CARD" (TD1)
    document_number: str | None = None
    surname: str | None = None
    given_names: str | None = None
    nationality: str | None = None
    birth_date: date | None = None
    expiry_date: date | None = None
    sex: str | None = None
    verified: bool = False
    #: Dígitos verificadores que cuadraron / que se revisaron (para la confianza que ve la empresa).
    checks_passed: int = 0
    checks_total: int = 0
    #: Campos legibles tal cual (nombre completo, número, etc.), listos para el expediente.
    fields: dict[str, str] = field(default_factory=dict)


def _check_digit(data: str) -> int:
    """Dígito verificador de ICAO 9303: suma de cada carácter por su peso cíclico (7,3,1), módulo 10."""
    total = sum(_VALUES.get(char, 0) * _WEIGHTS[index % 3] for index, char in enumerate(data))
    return total % 10


def _matches(data: str, digit: str) -> bool | None:
    """¿El dígito verificador cuadra? None si el dígito no es un número (ilegible):
    no cuenta ni a favor ni en contra."""
    if not digit.isdigit():
        return None
    return _check_digit(data) == int(digit)


def _names(block: str) -> tuple[str | None, str | None]:
    """Apellidos y nombres de un campo de nombre MRZ (`APELLIDO<<NOMBRE<SEGUNDO`): `<<` separa, `<` es espacio."""
    surname_raw, _, given_raw = block.partition("<<")
    surname = surname_raw.replace("<", " ").strip() or None
    given = given_raw.replace("<", " ").strip() or None
    return surname, given


def _mrz_date(digits: str, *, future: bool) -> date | None:
    """`YYMMDD` → fecha. El siglo se decide por el campo: la vigencia es futura o reciente; el nacimiento, pasado."""
    if len(digits) != 6 or not digits.isdigit():
        return None
    year, month, day = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
    current = business_today().year % 100
    century = 2000 if (future or year <= current) else 1900
    try:
        return date(century + year, month, day)
    except ValueError:
        return None


def _mrz_lines(text: str) -> list[str]:
    """Las líneas candidatas a MRZ: solo su alfabeto, al menos 28 caracteres y con relleno `<` (lo que la distingue de
    una línea de texto normal). Tesseract suele leer espacios dentro de la MRZ: se quitan antes de medir."""
    candidates: list[str] = []
    for raw in text.splitlines():
        cleaned = _ALLOWED.sub("", raw.replace(" ", "").upper())
        if len(cleaned) >= 28 and "<" in cleaned:
            candidates.append(cleaned)
    return candidates


def parse_mrz(text: str) -> MrzResult | None:
    """La MRZ que haya en `text` (pasaporte TD3 o identificación TD1), o None si no se reconoce ninguna. Mejor
    esfuerzo: nunca lanza y siempre devuelve lo que pudo leer."""
    lines = _mrz_lines(text)
    return _td3(lines) or _td1(lines)


def _td3(lines: list[str]) -> MrzResult | None:
    """Pasaporte: dos renglones de 44 (se aceptan 43-45 por el ruido de Tesseract). El primero empieza por `P`."""
    for first, second in pairwise(lines):
        if not first.startswith("P") or not (41 <= len(first) <= 46 and 41 <= len(second) <= 46):
            continue
        line2 = second.ljust(44, "<")
        number = line2[0:9].replace("<", "") or None
        birth = _mrz_date(line2[13:19], future=False)
        expiry = _mrz_date(line2[21:27], future=True)
        checks = [
            _matches(line2[0:9], line2[9]),
            _matches(line2[13:19], line2[19]),
            _matches(line2[21:27], line2[27]),
        ]
        surname, given = _names(first[5:].ljust(39, "<"))
        return _build(
            "PASSPORT", number, surname, given, line2[10:13].replace("<", "") or None, line2[20], birth, expiry, checks
        )
    return None


def _td1(lines: list[str]) -> MrzResult | None:
    """Identificación de tres renglones de 30 (la INE reciente al reverso). El número va en el primer renglón; la
    fecha de nacimiento, el sexo y la vigencia en el segundo; el nombre en el tercero."""
    for one, two, three in zip(lines, lines[1:], lines[2:], strict=False):
        if not (28 <= len(one) <= 32 and 28 <= len(two) <= 32 and 28 <= len(three) <= 32):
            continue
        if one[0] not in ("I", "A", "C"):  # tipo de documento TD1 (I = identificación)
            continue
        line1, line2 = one.ljust(30, "<"), two.ljust(30, "<")
        number = line1[5:14].replace("<", "") or None
        birth = _mrz_date(line2[0:6], future=False)
        expiry = _mrz_date(line2[8:14], future=True)
        checks = [_matches(line1[5:14], line1[14]), _matches(line2[0:6], line2[6]), _matches(line2[8:14], line2[14])]
        surname, given = _names(three.ljust(30, "<"))
        return _build(
            "ID_CARD", number, surname, given, line2[15:18].replace("<", "") or None, line2[7], birth, expiry, checks
        )
    return None


def _build(
    document_type: str,
    number: str | None,
    surname: str | None,
    given: str | None,
    nationality: str | None,
    sex_raw: str,
    birth: date | None,
    expiry: date | None,
    checks: list[bool | None],
) -> MrzResult:
    """Arma el resultado con los campos legibles y el conteo de dígitos verificadores que cuadraron."""
    sex = {"M": "M", "F": "F"}.get(sex_raw)
    reviewed = [value for value in checks if value is not None]
    passed = sum(1 for value in reviewed if value)
    fields: dict[str, str] = {}
    if number:
        fields["document_number"] = number
    full = " ".join(part for part in (given, surname) if part)
    if full:
        fields["full_name"] = full
    if nationality:
        fields["nationality"] = nationality
    if birth:
        fields["birth_date"] = birth.isoformat()
    if expiry:
        fields["expiry_date"] = expiry.isoformat()
    if sex:
        fields["sex"] = sex
    return MrzResult(
        document_type=document_type,
        document_number=number,
        surname=surname,
        given_names=given,
        nationality=nationality,
        birth_date=birth,
        expiry_date=expiry,
        sex=sex,
        verified=bool(reviewed) and passed == len(reviewed),
        checks_passed=passed,
        checks_total=len(reviewed),
        fields=fields,
    )
