"""Comparar lo que dijo el empleado con su dato registrado, con tolerancia (decisión del dueño, 2026-10-06).

Una transcripción nunca es exacta (acentos, mayúsculas, un apellido que no se dice, «Panificadora del Norte, S.A. de
C.V.» que nadie pronuncia completo), así que cada tipo de pregunta compara a su manera, sobre texto normalizado
(minúsculas, sin acentos ni signos; las palabras numéricas vueltas cifras con `numerals`):

- **Nombres** (nombre, apellidos, nombre completo, primer/segundo apellido, empresa, departamento, sitio): cada palabra
  significativa del dato debe oírse (parecida al menos `VOICE_TOKEN_SIMILARITY`, Levenshtein) y al menos
  `VOICE_NAME_MIN_RATIO` de ellas: un nombre completo se acepta sin el segundo apellido; un nombre distinto, no.
- **Fecha de nacimiento**: el día, el mes (nombre en el idioma, abreviado o en cifra) y el año (completo o sus dos
  últimas cifras) tienen que estar, en cualquier orden y formato («15 de mayo de 1990», «May 15, 1990»,
  «quinze mai quatre-vingt-dix», «15/05/1990»). También por separado: solo el mes (`BIRTH_MONTH`), solo el año
  (`BIRTH_YEAR`) o solo el día (`BIRTH_DAY`).
- **Suma** (`ARITHMETIC_SUM`): el total oído (con palabras o cifras) debe ser exactamente el esperado; decir solo los
  sumandos no basta (hay que dar el resultado).
- **Número de empleado**: las cifras, exactas y en orden («siete cuatro uno dos» o «siete mil cuatrocientos doce»);
  las letras del código (EMP-7412), parecidas o deletreadas.

El resultado lleva un puntaje (0-1) que la empresa ve en la revisión; la decisión (`ok`) es la del servidor.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

from app.core.config import settings
from app.models.enums import VoiceQuestion
from app.speech.engine import language_of
from app.speech.numerals import to_digits

#: Palabras que no distinguen un nombre (artículos, preposiciones y las siglas de una razón social).
_STOPWORDS = frozenset(
    {
        "de",
        "del",
        "la",
        "las",
        "los",
        "el",
        "y",
        "e",
        "da",
        "do",
        "das",
        "dos",
        "di",
        "du",
        "des",
        "le",
        "les",
        "der",
        "die",
        "und",
        "von",
        "the",
        "of",
        "and",
        "sa",
        "cv",
        "srl",
        "inc",
        "llc",
        "ltd",
        "ltda",
        "gmbh",
        "spa",
        "sas",
        "sarl",
        "ag",
        "sl",
        "co",
    }
)
#: Nombres de los meses por idioma de Whisper (sin acentos), en orden.
MONTHS: dict[str, tuple[str, ...]] = {
    "es": (
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ),
    "en": (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ),
    "pt": (
        "janeiro",
        "fevereiro",
        "marco",
        "abril",
        "maio",
        "junho",
        "julho",
        "agosto",
        "setembro",
        "outubro",
        "novembro",
        "dezembro",
    ),
    "fr": (
        "janvier",
        "fevrier",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "aout",
        "septembre",
        "octobre",
        "novembre",
        "decembre",
    ),
    "de": (
        "januar",
        "februar",
        "marz",
        "april",
        "mai",
        "juni",
        "juli",
        "august",
        "september",
        "oktober",
        "november",
        "dezember",
    ),
    "it": (
        "gennaio",
        "febbraio",
        "marzo",
        "aprile",
        "maggio",
        "giugno",
        "luglio",
        "agosto",
        "settembre",
        "ottobre",
        "novembre",
        "dicembre",
    ),
}
#: Otras formas de un mes que Whisper puede escribir.
_MONTH_ALIASES = {"es": {"setiembre": 9}, "de": {"janner": 1}, "pt": {"septembro": 9}}
_LETTERS_DIGITS = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")


@dataclass(frozen=True)
class Match:
    """Si la respuesta corresponde al dato y qué tan parecida fue (0-1)."""

    ok: bool
    score: float


def normalize(text: str) -> list[str]:
    """Palabras en minúsculas, sin acentos ni signos; una letra pegada a una cifra se separa («emp7» → «emp», «7»)."""
    folded = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()
    plain = _LETTERS_DIGITS.sub(" ", plain)
    return [word for word in re.split(r"[^a-z0-9]+", plain) if word]


def _similar(expected: str, heard: str) -> float:
    return SequenceMatcher(None, expected, heard).ratio()


def _heard_token(expected: str, heard: list[str], similarity: float) -> bool:
    """¿Alguna palabra oída es la esperada (o se le parece lo suficiente, o la contiene si es larga)?"""
    return any(_similar(expected, word) >= similarity or (len(expected) >= 4 and expected in word) for word in heard)


def match_name(expected: str, heard: str) -> Match:
    """Las palabras significativas del dato, oídas en cualquier orden."""
    tokens = [word for word in normalize(expected) if word not in _STOPWORDS and len(word) >= 2]
    tokens = tokens or normalize(expected)
    if not tokens:
        return Match(False, 0.0)
    said = normalize(heard)
    found = sum(_heard_token(token, said, settings.VOICE_TOKEN_SIMILARITY) for token in tokens)
    score = round(found / len(tokens), 3)
    return Match(score >= settings.VOICE_NAME_MIN_RATIO and found >= min(2, len(tokens)), score)


def _month_of(word: str, lang: str) -> int | None:
    names = MONTHS.get(lang, ())
    for index, name in enumerate(names, start=1):
        if word == name or (len(word) >= 3 and name.startswith(word)) or _similar(word, name) >= 0.85:
            return index
    return _MONTH_ALIASES.get(lang, {}).get(word)


def _take(numbers: list[int], wanted: int) -> bool:
    """Consume `wanted` de la lista si está (cada cifra oída cuenta una vez)."""
    if wanted in numbers:
        numbers.remove(wanted)
        return True
    return False


def match_date(expected: date, heard: str, locale: str) -> Match:
    """Día, mes y año de la fecha, dichos como sea en el idioma de la petición."""
    lang = language_of(locale)
    words = to_digits(normalize(heard), lang)
    numbers = [int(word) for word in words if word.isdigit()]
    month = next((m for word in words if not word.isdigit() and (m := _month_of(word, lang)) is not None), None)
    year = _take(numbers, expected.year) or _take(numbers, expected.year % 100)
    day = _take(numbers, expected.day)
    if month is None:
        month = expected.month if _take(numbers, expected.month) else None
    parts = (day, month == expected.month, year)
    return Match(all(parts), round(sum(parts) / 3, 3))


def _spoken_month(heard: str, lang: str) -> int | None:
    """El mes que se oyó: por su nombre en el idioma (abreviado o con una falta) o por su cifra."""
    words = to_digits(normalize(heard), lang)
    by_name = next((m for word in words if not word.isdigit() and (m := _month_of(word, lang)) is not None), None)
    if by_name is not None:
        return by_name
    numbers = [int(word) for word in words if word.isdigit()]
    return next((n for n in numbers if 1 <= n <= 12), None)


def match_month(expected: date, heard: str, locale: str) -> Match:
    """El mes de nacimiento, dicho por su nombre en el idioma o por su número (1-12)."""
    ok = _spoken_month(heard, language_of(locale)) == expected.month
    return Match(ok, 1.0 if ok else 0.0)


def match_year(expected: date, heard: str, locale: str) -> Match:
    """El año de nacimiento, completo («mil novecientos noventa», «1990») o sus dos últimas cifras («noventa»)."""
    numbers = [int(word) for word in to_digits(normalize(heard), language_of(locale)) if word.isdigit()]
    ok = expected.year in numbers or (expected.year % 100) in numbers
    return Match(ok, 1.0 if ok else 0.0)


def match_day(expected: date, heard: str, locale: str) -> Match:
    """El día de nacimiento, dicho con palabras o cifras (ordinal o cardinal: «quince», «el 15»)."""
    numbers = [int(word) for word in to_digits(normalize(heard), language_of(locale)) if word.isdigit()]
    ok = expected.day in numbers
    return Match(ok, 1.0 if ok else 0.0)


def match_sum(expected_total: int, heard: str, locale: str) -> Match:
    """La respuesta a la suma: el total oído (con palabras o cifras) debe ser exactamente el esperado. Decir solo los
    sumandos no basta (ninguno iguala al total, que es mayor que cada uno): hay que dar el resultado."""
    numbers = [int(word) for word in to_digits(normalize(heard), language_of(locale)) if word.isdigit()]
    ok = expected_total in numbers
    return Match(ok, 1.0 if ok else 0.0)


def match_code(expected: str, heard: str, locale: str) -> Match:
    """Un número de empleado: sus cifras exactas y, si tiene letras, parecidas o deletreadas."""
    words = to_digits(normalize(heard), language_of(locale))
    digits = "".join(ch for ch in expected if ch.isdigit())
    letters = "".join(word for word in normalize(expected) if word.isalpha())
    heard_digits = "".join(word for word in words if word.isdigit())
    heard_letters = "".join(word for word in words if word.isalpha())
    letters_ok = not letters or letters in heard_letters or _similar(letters, heard_letters) >= 0.6
    if not digits:  # un código solo de letras: dicho como palabra o deletreado
        spelled = Match(bool(letters) and letters_ok, 1.0 if letters_ok else 0.0)
        return spelled if spelled.ok else match_name(expected, heard)
    digits_ok = heard_digits == digits
    score = round((0.7 if digits_ok else 0.0) + (0.3 if letters_ok else 0.0), 3)
    return Match(digits_ok and letters_ok, score)


#: Qué comparación usa cada pregunta de fecha (todas con `expected` en ISO, «1990-05-15»).
_DATE_MATCHERS = {
    VoiceQuestion.BIRTH_DATE: match_date,
    VoiceQuestion.BIRTH_MONTH: match_month,
    VoiceQuestion.BIRTH_YEAR: match_year,
    VoiceQuestion.BIRTH_DAY: match_day,
}


def compare(kind: VoiceQuestion, expected: str, heard: str, locale: str) -> Match:
    """La comparación que corresponde a cada pregunta. La fecha y sus partes viajan en ISO (`expected` = «1990-05-15»);
    la suma, su total ya calculado; el número de empleado, su código; todo lo demás es un nombre."""
    if kind in _DATE_MATCHERS:
        return _DATE_MATCHERS[kind](date.fromisoformat(expected), heard, locale)
    if kind == VoiceQuestion.ARITHMETIC_SUM:
        return match_sum(int(expected), heard, locale)
    if kind == VoiceQuestion.EMPLOYEE_NUMBER:
        return match_code(expected, heard, locale)
    return match_name(expected, heard)
