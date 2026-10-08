"""Números dichos con palabras → cifras, en los idiomas de la plataforma (es, en, pt, fr, de, it).

Whisper suele escribir las cantidades con dígitos («7412», «1990»), pero no siempre («siete mil cuatrocientos doce»,
«nineteen ninety», «siebentausendvierhundertzwölf»): antes de comparar una fecha o un número de empleado con lo dicho,
cada secuencia de palabras numéricas se vuelve una cifra. Cubre cardinales hasta 9 999 (lo que cabe en un año o un
número de empleado), los ordinales con que se dice el día de una fecha y las particularidades de cada idioma: los
compuestos del alemán y del italiano (una sola palabra), las veintenas del francés («quatre-vingt-dix»), los años en
pares del inglés («nineteen ninety») y los conectores («y», «and», «et», «und», «e»). Y sabe dónde termina un número y
empieza otro: «siete cuatro uno dos» (un número dicho cifra por cifra) son cuatro cifras, no 14.

Todo trabaja sobre palabras ya normalizadas (`matching.normalize`: minúsculas y sin acentos).
"""

from collections.abc import Iterable

# Idioma de Whisper (`es`, `en`...) → sus palabras numéricas: palabra → valor; `100` y `1000` son las palabras de
# centena y millar sueltas (las centenas con valor propio, p. ej. «doscientos», van con su valor).
_ES_UNITS = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21,
    "veintiuno": 21, "veintiuna": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25,
    "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29, "treinta": 30, "cuarenta": 40,
    "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90, "cien": 100, "ciento": 100,
    "doscientos": 200, "doscientas": 200, "trescientos": 300, "trescientas": 300, "cuatrocientos": 400,
    "cuatrocientas": 400, "quinientos": 500, "quinientas": 500, "seiscientos": 600, "seiscientas": 600,
    "setecientos": 700, "setecientas": 700, "ochocientos": 800, "ochocientas": 800, "novecientos": 900,
    "novecientas": 900, "mil": 1000, "primero": 1, "primer": 1,
}  # fmt: skip
_EN_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000, "first": 1,
    "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
    "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20, "thirtieth": 30,
}  # fmt: skip
_PT_UNITS = {
    "zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7,
    "oito": 8, "nove": 9, "dez": 10, "onze": 11, "doze": 12, "treze": 13, "catorze": 14, "quatorze": 14,
    "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18, "dezenove": 19, "vinte": 20, "trinta": 30,
    "quarenta": 40, "cinquenta": 50, "sessenta": 60, "setenta": 70, "oitenta": 80, "noventa": 90, "cem": 100,
    "cento": 100, "duzentos": 200, "duzentas": 200, "trezentos": 300, "trezentas": 300, "quatrocentos": 400,
    "quatrocentas": 400, "quinhentos": 500, "quinhentas": 500, "seiscentos": 600, "seiscentas": 600,
    "setecentos": 700, "setecentas": 700, "oitocentos": 800, "oitocentas": 800, "novecentos": 900,
    "novecentas": 900, "mil": 1000, "primeiro": 1,
}  # fmt: skip
_FR_UNITS = {
    "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8,
    "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16,
    "vingt": 20, "vingts": 20, "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60, "cent": 100,
    "cents": 100, "mille": 1000, "premier": 1,
}  # fmt: skip
_DE_UNITS = {
    "null": 0, "ein": 1, "eins": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4, "funf": 5, "sechs": 6, "sieben": 7,
    "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwolf": 12, "dreizehn": 13, "vierzehn": 14, "funfzehn": 15,
    "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19, "zwanzig": 20, "dreissig": 30, "vierzig": 40,
    "funfzig": 50, "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90, "hundert": 100, "tausend": 1000,
    "erst": 1, "dritt": 3, "siebt": 7,
}  # fmt: skip
_IT_UNITS = {
    "zero": 0, "un": 1, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7,
    "otto": 8, "nove": 9, "dieci": 10, "undici": 11, "dodici": 12, "tredici": 13, "quattordici": 14, "quindici": 15,
    "sedici": 16, "diciassette": 17, "diciotto": 18, "diciannove": 19, "venti": 20, "vent": 20, "trenta": 30,
    "trent": 30, "quaranta": 40, "quarant": 40, "cinquanta": 50, "cinquant": 50, "sessanta": 60, "sessant": 60,
    "settanta": 70, "settant": 70, "ottanta": 80, "ottant": 80, "novanta": 90, "novant": 90, "cento": 100,
    "duecento": 200, "trecento": 300, "quattrocento": 400, "cinquecento": 500, "seicento": 600, "settecento": 700,
    "ottocento": 800, "novecento": 900, "mille": 1000, "mila": 1000, "primo": 1,
}  # fmt: skip
WORDS: dict[str, dict[str, int]] = {
    "es": _ES_UNITS,
    "en": _EN_UNITS,
    "pt": _PT_UNITS,
    "fr": _FR_UNITS,
    "de": _DE_UNITS,
    "it": _IT_UNITS,
}
#: Conectores dentro de un número («treinta y uno», «one hundred and five», «soixante et onze», «mil novecentos e
#: noventa»); en alemán va pegado («einundzwanzig»).
CONNECTORS: dict[str, frozenset[str]] = {
    "es": frozenset({"y"}),
    "en": frozenset({"and"}),
    "pt": frozenset({"e"}),
    "fr": frozenset({"et"}),
    "de": frozenset({"und"}),
    "it": frozenset({"e"}),
}
#: Idiomas que pegan las palabras de un número en una sola («siebentausend…», «millenovecento…»).
COMPOUND = frozenset({"de", "it"})
#: Terminaciones de los ordinales del alemán («fünfzehnter», «ersten») que se quitan antes de buscar la palabra.
_DE_ORDINAL_SUFFIXES = ("sten", "ster", "stes", "ste", "ten", "ter", "tes", "te")
#: Primeras cifras de un año dicho en pares («nineteen ninety», «twenty twenty-five»).
_YEAR_PAIR_HEADS = (19, 20)


def _lookup(word: str, table: dict[str, int]) -> int | None:
    """El valor de una palabra numérica (también un ordinal del alemán con su terminación quitada)."""
    if word in table:
        return table[word]
    if table is _DE_UNITS:
        for suffix in _DE_ORDINAL_SUFFIXES:
            if word.endswith(suffix) and word[: -len(suffix)] in table:
                return table[word[: -len(suffix)]]
    return None


def split_compound(word: str, lang: str) -> list[str] | None:
    """Una palabra compuesta del alemán o del italiano en sus palabras numéricas («millenovecentonovanta» → mille,
    novecento, novanta): la más larga que encaje primero; None si algo de la palabra no es un número."""
    table = WORDS[lang]
    pieces: list[str] = []
    rest = word
    while rest:
        found = next(
            (
                rest[:length]
                for length in range(len(rest), 0, -1)
                if rest[:length] in table or rest[:length] in CONNECTORS[lang]
            ),
            None,
        )
        if found is None:
            return None
        pieces.append(found)
        rest = rest[len(found) :]
    return pieces if len(pieces) > 1 else None


def _is_ten(value: int) -> bool:
    return 20 <= value <= 90 and value % 10 == 0


class _Accumulator:
    """Arma una cifra con las palabras que van llegando (la lógica usual de centenas y millares) y sabe cuándo una
    palabra ya no continúa el número anterior («treinta y uno veintiuno» son dos: 31 y 21)."""

    def __init__(self, lang: str) -> None:
        self.lang = lang
        self.total = 0
        self.current = 0
        self.started = False
        self.last: int | None = None

    def continues(self, value: int) -> bool:
        """¿`value` sigue el número en curso? Una centena o un millar siempre; una cifra menor tras una centena o un
        millar («ciento cinco», «mil novecientos noventa»); una unidad tras una decena («treinta y uno»); en alemán la
        unidad va antes de la decena («einundzwanzig»); en francés las decenas 60 y 80 llevan de 10 a 19 detrás
        («soixante-dix», «quatre-vingt-onze») y «quatre» seguido de «vingt» es 80; en inglés y alemán un año se dice
        en pares («nineteen ninety», «twenty twenty-five»). Lo demás («siete cuatro», «quince diecinueve») empieza
        otro número."""
        if not self.started or value >= 100:
            return True
        last = self.last or 0
        if self.current == 0 or (self.current >= 100 and self.current % 100 == 0):
            return True  # tras un millar («siete mil cuatro…») o una centena («ciento cinco»)
        if _is_ten(last) and 1 <= value <= 9:
            return True
        if self.lang == "de" and 1 <= last <= 9 and _is_ten(value):
            return True
        french_teens = self.current % 100 in (60, 80) and 10 <= value <= 19
        if self.lang == "fr" and (french_teens or (last == 4 and value == 20)):
            return True
        return self.lang in ("en", "de") and self._year_pair(value)

    def _year_pair(self, value: int) -> bool:
        return self.last in _YEAR_PAIR_HEADS and self.current == self.last and 10 <= value <= 99

    def add(self, value: int) -> None:
        self.started = True
        if value == 1000:
            self.total += (self.current or 1) * 1000
            self.current = 0
        elif value == 100:
            self.current = (self.current or 1) * 100
        elif self.lang == "fr" and value == 20 and self.last == 4:
            self.current += 76  # «quatre-vingt(s)»: 4 → 80
        elif self.lang in ("en", "de") and self._year_pair(value):
            self.current = self.current * 100 + value  # un año en pares
        else:
            self.current += value
        self.last = value

    def flush(self) -> str | None:
        if not self.started:
            return None
        value = str(self.total + self.current)
        self.total, self.current, self.started, self.last = 0, 0, False, None
        return value


def to_digits(tokens: Iterable[str], lang: str) -> list[str]:
    """Las palabras con cada número dicho con palabras vuelto una cifra; lo que no es un número queda igual. Un idioma
    que no se conoce no cambia nada."""
    table = WORDS.get(lang)
    if table is None:
        return list(tokens)
    out: list[str] = []
    number = _Accumulator(lang)
    for token in tokens:
        words = [token]
        if lang in COMPOUND and _lookup(token, table) is None:
            words = split_compound(token, lang) or [token]
        for word in words:
            value = _lookup(word, table)
            if value is not None:
                if not number.continues(value):
                    out.append(number.flush() or "")
                number.add(value)
            elif word in CONNECTORS[lang] and number.started:
                continue
            else:
                flushed = number.flush()
                if flushed is not None:
                    out.append(flushed)
                out.append(word)
    flushed = number.flush()
    if flushed is not None:
        out.append(flushed)
    return out
