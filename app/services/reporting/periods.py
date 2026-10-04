"""Periodos de tiempo en español: «hoy», «la semana pasada», «del 1 al 15 de marzo», «en 2025»...

Todo en la hora del negocio (APP_TIMEZONE), no la del servidor: «hoy» es el día de la empresa. Un
periodo es [inicio, fin) — el fin no se incluye — y lleva su nombre para la respuesta.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings

MONTHS = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)  # fmt: skip
_MONTH_INDEX = {name: number for number, name in enumerate(MONTHS, start=1)} | {"setiembre": 9}
_WEEKDAYS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}
_UNITS = {"dia": "day", "dias": "day", "semana": "week", "semanas": "week", "mes": "month", "meses": "month"}
_UNITS |= {"ano": "year", "anos": "year"}

_MONTH = "(" + "|".join(sorted(_MONTH_INDEX, key=len, reverse=True)) + ")"
_DATE = r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?"
_YEAR = r"(?:\s+(?:de\s+|del\s+)?(\d{4}))?"
_NUM = (
    r"(\d+|un|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|quince|veinte|treinta|sesenta|noventa)"
)
_NUM_WORDS = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7}
_NUM_WORDS |= {"ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "quince": 15, "veinte": 20, "treinta": 30}
_NUM_WORDS |= {"sesenta": 60, "noventa": 90}


@dataclass(frozen=True)
class Period:
    """[start, end) en la hora del negocio. `start` None = desde el principio."""

    start: datetime | None
    end: datetime
    label: str

    def previous(self) -> Period | None:
        """El periodo inmediato anterior del mismo largo (para comparar). None si no tiene inicio."""
        if self.start is None:
            return None
        length = self.end - self.start
        return Period(self.start - length, self.start, "el periodo anterior")


@dataclass(frozen=True)
class FoundPeriod:
    period: Period
    #: Lo que se reconoció en el texto (para no volver a interpretarlo).
    span: tuple[int, int]


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.APP_TIMEZONE)


def day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=_zone())


def _days(first: date, last: date, label: str) -> Period:
    """Del día `first` al `last`, ambos completos."""
    return Period(day_start(first), day_start(last + timedelta(days=1)), label)


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def spoken(day: date, *, year: bool = True) -> str:
    """3 de octubre de 2026."""
    text = f"{day.day} de {MONTHS[day.month - 1]}"
    return f"{text} de {day.year}" if year else text


def _span(first: date, last: date) -> str:
    if first == last:
        return spoken(first)
    if first.year == last.year and first.month == last.month:
        return f"del {first.day} al {spoken(last)}"
    return f"del {spoken(first, year=first.year != last.year)} al {spoken(last)}"


def _year(text: str | None, today: date) -> int:
    if not text:
        return today.year
    value = int(text)
    return value + 2000 if value < 100 else value


def _number(text: str) -> int:
    return int(text) if text.isdigit() else _NUM_WORDS[text]


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# ---------- Reconocedores (en orden: el primero que encuentra gana) ----------


def _month_range(text: str, today: date) -> FoundPeriod | None:
    """«del 1 al 15 de marzo», «entre el 3 de enero y el 9 de febrero de 2026»."""
    pattern = (
        rf"\b(?:del?|entre(?:\s+el)?)\s+(\d{{1,2}})(?:\s+de\s+{_MONTH})?{_YEAR}"
        rf"\s+(?:al|y(?:\s+el)?|hasta(?:\s+el)?|a)\s+(\d{{1,2}})\s+de\s+{_MONTH}{_YEAR}"
    )
    match = re.search(pattern, text)
    if not match:
        return None
    d1, m1, y1, d2, m2, y2 = match.groups()
    year2 = _year(y2, today)
    first = _safe_date(_year(y1, today) if y1 else year2, _MONTH_INDEX[m1 or m2], int(d1))
    last = _safe_date(year2, _MONTH_INDEX[m2], int(d2))
    if first is None or last is None or first > last:
        return None
    return FoundPeriod(_days(first, last, _span(first, last)), match.span())


def _parse_numeric(match: Sequence[str | None], today: date) -> date | None:
    day, month, year = match
    return _safe_date(_year(year, today), int(month or 0), int(day or 0))


def _numeric_range(text: str, today: date) -> FoundPeriod | None:
    """«del 01/03/2026 al 15/03/2026», «entre 1/3 y 15/3»."""
    pattern = rf"\b(?:del?|entre(?:\s+el)?|desde(?:\s+el)?)\s+{_DATE}\s+(?:al|y(?:\s+el)?|hasta(?:\s+el)?|a)\s+{_DATE}"
    match = re.search(pattern, text)
    if not match:
        return None
    groups = match.groups()
    first, last = _parse_numeric(groups[:3], today), _parse_numeric(groups[3:], today)
    if first is None or last is None or first > last:
        return None
    return FoundPeriod(_days(first, last, _span(first, last)), match.span())


def _since_until(text: str, today: date) -> FoundPeriod | None:
    """«desde el 1 de marzo», «hasta el 15/03/2026»."""
    pattern = rf"\b(desde|a partir|hasta)(?:\s+del?)?(?:\s+el)?\s+(?:{_DATE}|(\d{{1,2}})\s+de\s+{_MONTH}{_YEAR})"
    match = re.search(pattern, text)
    if not match:
        return None
    word, d, m, y, d2, month, y2 = match.groups()
    day = _parse_numeric((d, m, y), today) if d else _safe_date(_year(y2, today), _MONTH_INDEX[str(month)], int(d2))
    if day is None:
        return None
    tomorrow = day_start(today + timedelta(days=1))
    if word == "hasta":
        return FoundPeriod(Period(None, day_start(day + timedelta(days=1)), f"hasta el {spoken(day)}"), match.span())
    return FoundPeriod(Period(day_start(day), tomorrow, f"desde el {spoken(day)}"), match.span())


def _single_date(text: str, today: date) -> FoundPeriod | None:
    """«el 15/03/2026», «el 3 de marzo»."""
    match = re.search(rf"\b{_DATE}\b", text)
    if match:
        day = _parse_numeric(match.groups(), today)
        if day is not None:
            return FoundPeriod(_days(day, day, spoken(day)), match.span())
    match = re.search(rf"\b(\d{{1,2}})\s+de\s+{_MONTH}{_YEAR}", text)
    if match:
        d, month, y = match.groups()
        day = _safe_date(_year(y, today), _MONTH_INDEX[month], int(d))
        if day is not None:
            return FoundPeriod(_days(day, day, spoken(day)), match.span())
    return None


def _relative_day(text: str, today: date) -> FoundPeriod | None:
    for pattern, offset, name in (
        (r"\b(?:antier|anteayer|antes de ayer)\b", 2, "antier"),
        (r"\bayer\b", 1, "ayer"),
        (r"\bhoy\b", 0, "hoy"),
    ):
        match = re.search(pattern, text)
        if match:
            day = today - timedelta(days=offset)
            return FoundPeriod(_days(day, day, f"{name} ({spoken(day)})"), match.span())
    return None


def _calendar(text: str, today: date) -> FoundPeriod | None:
    """Esta semana / la semana pasada / este mes / el mes pasado / este trimestre / este año..."""
    monday = today - timedelta(days=today.weekday())
    month = _month_start(today.year, today.month)
    quarter = _month_start(today.year, 3 * ((today.month - 1) // 3) + 1)
    options: tuple[tuple[str, date, date, str], ...] = (
        (r"\b(?:la\s+)?semana\s+(?:pasada|anterior)\b", monday - timedelta(days=7), monday, "la semana pasada"),
        (r"\b(?:esta|la presente|en la)\s+semana\b", monday, monday + timedelta(days=7), "esta semana"),
        (r"\b(?:el\s+)?mes\s+(?:pasado|anterior)\b", _add_months(month, -1), month, ""),
        (r"\b(?:este|el presente|en el)\s+mes\b|\bmes actual\b", month, _next_month(month), ""),
        (r"\b(?:el\s+)?trimestre\s+(?:pasado|anterior)\b", _add_months(quarter, -3), quarter, "el trimestre pasado"),
        (r"\b(?:este|el presente)\s+trimestre\b", quarter, _add_months(quarter, 3), "este trimestre"),
        (r"\b(?:el\s+)?ano\s+(?:pasado|anterior)\b", date(today.year - 1, 1, 1), date(today.year, 1, 1), ""),
        (
            r"\b(?:este|el presente|en el)\s+ano\b|\bano actual\b",
            date(today.year, 1, 1),
            date(today.year + 1, 1, 1),
            "",
        ),
    )
    for pattern, first, end, label in options:
        match = re.search(pattern, text)
        if match:
            if not label:  # meses y años se nombran por sí mismos
                label = f"{MONTHS[first.month - 1]} de {first.year}" if (end - first).days < 32 else str(first.year)
            return FoundPeriod(Period(day_start(first), day_start(end), label), match.span())
    return None


def _last_units(text: str, today: date) -> FoundPeriod | None:
    """«los últimos 30 días», «última semana», «últimos 3 meses»."""
    match = re.search(rf"\bultim[oa]s?\s+(?:{_NUM}\s+)?(dias?|semanas?|mes(?:es)?|anos?)\b", text)
    if not match:
        return None
    amount = _number(match.group(1)) if match.group(1) else 1
    unit = _UNITS[match.group(2)]
    days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * amount
    first = today - timedelta(days=days - 1)
    plural = {"day": "días", "week": "semanas", "month": "meses", "year": "años"}[unit]
    single = {"day": "último día", "week": "última semana", "month": "último mes", "year": "último año"}[unit]
    label = f"últimos {amount} {plural}" if amount != 1 else single
    return FoundPeriod(_days(first, today, label), match.span())


def _ago(text: str, today: date) -> FoundPeriod | None:
    """«hace 3 días» (ese día), «hace 2 semanas» (esa semana), «hace un mes» (ese mes)."""
    match = re.search(rf"\bhace\s+{_NUM}\s+(dias?|semanas?|mes(?:es)?)\b", text)
    if not match:
        return None
    amount, unit = _number(match.group(1)), _UNITS[match.group(2)]
    if unit == "day":
        day = today - timedelta(days=amount)
        return FoundPeriod(_days(day, day, spoken(day)), match.span())
    if unit == "week":
        monday = today - timedelta(days=today.weekday() + 7 * amount)
        return FoundPeriod(
            _days(monday, monday + timedelta(days=6), _span(monday, monday + timedelta(days=6))), match.span()
        )
    first = _add_months(_month_start(today.year, today.month), -amount)
    return FoundPeriod(
        Period(day_start(first), day_start(_next_month(first)), f"{MONTHS[first.month - 1]} de {first.year}"),
        match.span(),
    )


def _month_name(text: str, today: date) -> FoundPeriod | None:
    """«en marzo», «marzo de 2025»: sin año, el marzo más reciente (este año o el anterior)."""
    match = re.search(rf"\b{_MONTH}{_YEAR}\b", text)
    if not match:
        return None
    month, year = _MONTH_INDEX[match.group(1)], match.group(2)
    value = int(year) if year else (today.year if month <= today.month else today.year - 1)
    first = _month_start(value, month)
    return FoundPeriod(
        Period(day_start(first), day_start(_next_month(first)), f"{MONTHS[month - 1]} de {value}"), match.span()
    )


def _year_only(text: str, _today: date) -> FoundPeriod | None:
    match = re.search(r"\b(?:en|del|durante|de)\s+(?:el\s+)?(?:ano\s+)?(20\d{2})\b", text)
    if not match:
        return None
    year = int(match.group(1))
    return FoundPeriod(Period(day_start(date(year, 1, 1)), day_start(date(year + 1, 1, 1)), str(year)), match.span())


def _weekday(text: str, today: date) -> FoundPeriod | None:
    """«el lunes» (el más reciente), «el viernes pasado»."""
    match = re.search(r"\b(?:el\s+)?(lunes|martes|miercoles|jueves|viernes|sabado|domingo)(\s+pasado)?\b", text)
    if not match:
        return None
    back = (today.weekday() - _WEEKDAYS[match.group(1)]) % 7
    if match.group(2) and back == 0:
        back = 7
    day = today - timedelta(days=back)
    return FoundPeriod(
        _days(
            day, day, f"el {match.group(1)} {spoken(day)}".replace("miercoles", "miércoles").replace("sabado", "sábado")
        ),
        match.span(),
    )


_FINDERS = (
    _month_range,
    _numeric_range,
    _since_until,
    _relative_day,
    _calendar,
    _last_units,
    _ago,
    _single_date,
    _month_name,
    _year_only,
    _weekday,
)


def find_period(text: str, today: date) -> FoundPeriod | None:
    """El periodo que menciona el texto normalizado (o None si no menciona ninguno)."""
    for finder in _FINDERS:
        found = finder(text, today)
        if found is not None:
            return found
    return None
