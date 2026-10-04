"""Lenguaje: normalizar texto en español y reconocer palabras aunque tengan errores de dedo.

La gente escribe con y sin acentos, en plural o singular y con errores («empelados»). Todo se
compara normalizado (minúsculas, sin acentos ni signos) y por raíz (sin plural); las palabras
largas se aceptan con un parecido alto (`similar`).
"""

import re
import unicodedata
from difflib import SequenceMatcher

#: Palabras que no dicen de qué datos se trata (conectores, verbos de petición, cortesía).
_STOPWORDS_TEXT = """
a al algo algun alguna algunos ante aqui asi aun cada como con cual cuales cuando de del desde
donde dos el ella ellos en entre era es esa ese eso esos esas esta este esto estos estas fue
fueron ha han hay hasta la las le les lo los me mi mis muy ni no nos o otra otro otros para pero
por porque que quien quienes se sea ser si sin sobre son su sus tambien te tiene tienen tengo
tenemos todo todos todas tu un una unas uno unos y ya yo dame dime muestrame mostrar muestra
muestrales quiero necesito ver lista listar listado reporte reportes informe informes datos dato
favor podrias puedes puede saber cuanto cuantos cuantas cuantas total numero cantidad hubo habia
han hecho hace genera generar generame haz hazme sacar saca sacame obtener obten dar
"""
STOPWORDS = frozenset(_STOPWORDS_TEXT.split())

_NUMBERS = {
    "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "veinte": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "noventa": 90, "cien": 100,
}  # fmt: skip
_NON_WORD = re.compile(r"[^a-z0-9/:\-.]+")
_EDGE_PUNCT = re.compile(r"(^[\-.:/]+|[\-.:/]+$)")


def normalize(text: str) -> str:
    """«¿Cuántos Empleados hay en Producción?» → «cuantos empleados hay en produccion»."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    words = (_EDGE_PUNCT.sub("", word) for word in _NON_WORD.sub(" ", plain).split())
    return " ".join(word for word in words if word)


def tokens(text: str) -> list[str]:
    return normalize(text).split()


def stem(word: str) -> str:
    """Raíz sin plural: validadores → validador, identificaciones → identificacion, areas → area."""
    if len(word) <= 3 or not word.isalpha():
        return word
    if word.endswith("ces"):
        return word[:-3] + "z"
    if word.endswith(("iones", "ores", "eres", "ales", "anes", "enes", "ines", "dades", "eses")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def similar(a: str, b: str) -> bool:
    """¿Es la misma palabra con un error de dedo? Solo palabras largas (en las cortas un cambio de
    letra es otra palabra: «activo» / «inactivo» no se confunden)."""
    if a == b:
        return True
    if min(len(a), len(b)) < 7 or abs(len(a) - len(b)) > 2:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.86


def phrase_stems(phrase: str) -> tuple[str, ...]:
    return tuple(stem(word) for word in tokens(phrase))


def find_phrase(words: list[str], phrase: tuple[str, ...], *, fuzzy: bool = False) -> int:
    """Posición donde aparece la frase (por raíces) en las palabras de la pregunta; -1 si no está."""
    if not phrase or len(phrase) > len(words):
        return -1
    for start in range(len(words) - len(phrase) + 1):
        window = words[start : start + len(phrase)]
        if all(w == p or (fuzzy and similar(w, p)) for w, p in zip(window, phrase, strict=True)):
            return start
    return -1


def number_at(word: str) -> int | None:
    """«10» o «diez» → 10."""
    if word.isdigit():
        return int(word)
    return _NUMBERS.get(word)


def content_words(words: list[str]) -> list[str]:
    """Las palabras que sí dicen algo (sin conectores, números ni palabras de una letra)."""
    return [w for w in words if w not in STOPWORDS and len(w) > 2 and not any(ch.isdigit() for ch in w)]
