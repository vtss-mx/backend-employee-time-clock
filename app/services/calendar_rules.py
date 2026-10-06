"""Reglas del calendario de días libres (funciones puras, sin BD).

- Festivos oficiales de México (Ley Federal del Trabajo, art. 74, con la reforma de 2024): 1 de
  enero; primer lunes de febrero; tercer lunes de marzo; 1 de mayo; 16 de septiembre; tercer lunes
  de noviembre; 25 de diciembre; y cada seis años el día de la transmisión del Poder Ejecutivo
  Federal (1 de octubre desde 2024: 2024, 2030, 2036...; hasta 2018 era el 1 de diciembre). La
  jornada electoral no se incluye: cambia con cada elección (la empresa la agrega como festivo).
- Un día es libre para un empleado si es festivo de la empresa o lo cubre una ausencia APROBADA,
  salvo que la empresa le haya marcado ese día como laborable. El día que cuenta es el de la jornada:
  un turno nocturno es del día en que entra.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.i18n import Text

#: Clase de día libre de un festivo (las ausencias usan el código de su tipo: VACATION, PERMISSION...).
HOLIDAY = "HOLIDAY"
#: Primer año de la transmisión del Poder Ejecutivo el 1 de octubre (reforma de 2024); se repite cada 6.
TRANSMISSION_YEAR = 2024
TRANSMISSION_EVERY = 6
MONDAY = 0


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """El `n`-ésimo día de la semana `weekday` (lunes = 0) del mes: p. ej. el tercer lunes de marzo."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def official_holidays(year: int) -> list[tuple[date, str]]:
    """Los días de descanso obligatorio del año (art. 74 de la LFT), en orden, con la llave de su nombre (el
    catálogo de mensajes lo tiene en cada idioma; al agregarlos se guardan en el idioma de quien los agrega)."""
    days = [
        (date(year, 1, 1), "HOLIDAY_NEW_YEAR"),
        (nth_weekday(year, 2, MONDAY, 1), "HOLIDAY_CONSTITUTION"),
        (nth_weekday(year, 3, MONDAY, 3), "HOLIDAY_BENITO_JUAREZ"),
        (date(year, 5, 1), "HOLIDAY_LABOR_DAY"),
        (date(year, 9, 16), "HOLIDAY_INDEPENDENCE"),
        (nth_weekday(year, 11, MONDAY, 3), "HOLIDAY_REVOLUTION"),
        (date(year, 12, 25), "HOLIDAY_CHRISTMAS"),
    ]
    if (year - TRANSMISSION_YEAR) % TRANSMISSION_EVERY == 0:
        handover = date(year, 10, 1) if year >= TRANSMISSION_YEAR else date(year, 12, 1)
        days.append((handover, "HOLIDAY_TRANSMISSION"))
    return sorted(days)


def span_days(starts_on: date, ends_on: date) -> int:
    """Días de un rango con ambos extremos incluidos."""
    return (ends_on - starts_on).days + 1


def overlaps(start: date, end: date, other_start: date, other_end: date) -> bool:
    """Dos rangos de días (ambos extremos incluidos) comparten al menos un día."""
    return start <= other_end and other_start <= end


@dataclass(frozen=True)
class DayOff:
    """Por qué un día no se trabaja: un festivo (un solo día) o una ausencia aprobada (su rango)."""

    kind: str
    name: str
    starts_on: date
    ends_on: date
    #: Cómo se le dice al empleado ("Estás de vacaciones", del catálogo en el idioma de la petición); los festivos
    #: usan su propio texto.
    phrase: str = ""

    def explain(self, work_date: date, today: date) -> Text:
        """El motivo para el empleado: "Hoy es día festivo: Navidad", "Estás de vacaciones del ... al ..."."""
        if self.kind == HOLIDAY:
            if work_date == today:
                return Text("HOLIDAY_TODAY", {"name": self.name})
            return Text("HOLIDAY_ON", {"date": work_date, "name": self.name})
        if self.starts_on == self.ends_on:
            return Text("DAY_OFF_ON", {"phrase": self.phrase, "date": self.starts_on})
        return Text("DAY_OFF_RANGE", {"phrase": self.phrase, "start": self.starts_on, "end": self.ends_on})


@dataclass(frozen=True)
class DaysOff:
    """Los días libres de unos empleados en un rango de fechas (lo arma una consulta por tabla).

    `holidays`: festivos de la empresa por fecha; `absences`: ausencias aprobadas de cada empleado;
    `workdays`: (empleado, fecha) que la empresa marcó como laborables.
    """

    holidays: Mapping[date, str] = field(default_factory=dict)
    absences: Mapping[int, list[DayOff]] = field(default_factory=dict)
    workdays: Collection[tuple[int, date]] = field(default_factory=frozenset)

    def on(self, employee_id: int, day: date) -> DayOff | None:
        """El motivo por el que el empleado no trabaja ese día (None: es laborable para él)."""
        return day_off_on(day, self.holidays, self.absences.get(employee_id, ()), employee_id, self.workdays)


def day_off_on(
    day: date,
    holidays: Mapping[date, str],
    absences: Iterable[DayOff],
    employee_id: int,
    workdays: Collection[tuple[int, date]],
) -> DayOff | None:
    """La regla: festivo o ausencia aprobada que cubre el día, salvo que el día sea laborable para él."""
    if (employee_id, day) in workdays:
        return None
    if day in holidays:
        return DayOff(HOLIDAY, holidays[day], day, day)
    return next((absence for absence in absences if absence.starts_on <= day <= absence.ends_on), None)
