"""Reglas de horario de un turno: cuándo ocurre, cuándo se puede checar y cómo se cuenta.

Funciones puras (sin BD), en la hora del negocio:

- Un turno ocurre los días de su semana (`weekdays`, bits lunes = 1 ... domingo = 64). Si la salida
  es igual o anterior a la entrada, termina al día siguiente (turno nocturno): su jornada es del día
  en que ENTRA.
- Se puede checar la entrada desde `early_check_in_minutes` antes de la hora de entrada; la salida,
  hasta `late_check_out_minutes` después de la hora de salida. Esa ventana decide a qué turno
  pertenece un registro: a las 2:00 del martes, un turno de 22:00 a 6:00 que entró el lunes.
- Retardo: minutos después de la entrada si pasan de `late_tolerance_minutes` (dentro de la
  tolerancia no cuenta). Salida anticipada: igual con `early_check_out_minutes` antes de la salida.
- Turnos que se cruzan (el nocturno de ayer con su salida tardía y el matutino de hoy con su
  entrada temprana): se elige el de entrada programada más cercana al momento del registro.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

MINUTES_PER_DAY = 24 * 60
#: Nombre de cada día (lunes = 0, como date.weekday()).
WEEKDAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


class ClockTimes(Protocol):
    """La entrada y la salida de un turno (el modelo `Shift` o un esquema de entrada)."""

    start_time: time
    end_time: time


class CheckWindow(ClockTimes, Protocol):
    """Además, cuánto antes se entra y hasta cuándo se sale."""

    early_check_in_minutes: int
    late_check_out_minutes: int


class ShiftTimes(CheckWindow, Protocol):
    """Todo lo que las reglas necesitan del turno guardado."""

    weekdays: int
    late_tolerance_minutes: int
    early_check_out_minutes: int


def day_bit(day: date) -> int:
    """El bit del día en una máscara de la semana (lunes = 1 ... domingo = 64)."""
    return 1 << day.weekday()


def weekdays_of(mask: int) -> list[int]:
    """[0, 2, 4] = lunes, miércoles y viernes."""
    return [day for day in range(7) if mask & (1 << day)]


def mask_of(days: Iterable[int]) -> int:
    """Máscara de la semana a partir de los días (lunes = 0)."""
    mask = 0
    for day in days:
        mask |= 1 << day
    return mask


def overnight(shift: ClockTimes) -> bool:
    """La salida es igual o anterior a la entrada: termina al día siguiente."""
    return shift.end_time <= shift.start_time


def duration_minutes(shift: ClockTimes) -> int:
    start = shift.start_time.hour * 60 + shift.start_time.minute
    end = shift.end_time.hour * 60 + shift.end_time.minute
    return end - start if end > start else end + MINUTES_PER_DAY - start


def window_minutes(shift: CheckWindow) -> int:
    """Minutos desde que se puede checar la entrada hasta el último momento para checar la salida."""
    return shift.early_check_in_minutes + duration_minutes(shift) + shift.late_check_out_minutes


@dataclass(frozen=True)
class Occurrence:
    """Un turno en un día concreto: su jornada."""

    work_date: date
    start: datetime
    end: datetime
    #: Desde cuándo se puede checar la entrada y hasta cuándo la salida.
    opens: datetime
    deadline: datetime

    def contains(self, moment: datetime) -> bool:
        return self.opens <= moment <= self.deadline


def occurrence_of(shift: ShiftTimes, work_date: date, zone: ZoneInfo) -> Occurrence:
    """La jornada del turno que empieza `work_date` (en la hora del negocio)."""
    start = datetime.combine(work_date, shift.start_time, zone)
    end_date = work_date + timedelta(days=1) if overnight(shift) else work_date
    end = datetime.combine(end_date, shift.end_time, zone)
    return Occurrence(
        work_date=work_date,
        start=start,
        end=end,
        opens=start - timedelta(minutes=shift.early_check_in_minutes),
        deadline=end + timedelta(minutes=shift.late_check_out_minutes),
    )


def works_on(shift: ShiftTimes, day: date) -> bool:
    return bool(shift.weekdays & day_bit(day))


def late_minutes(shift: ShiftTimes, occurrence: Occurrence, check_in: datetime) -> int:
    """Minutos de retardo; dentro de la tolerancia, cero."""
    late = int((check_in - occurrence.start).total_seconds() // 60)
    return late if late > shift.late_tolerance_minutes else 0


def minutes_between(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


def closest(occurrences: Iterable[Occurrence], moment: datetime) -> Occurrence | None:
    """La jornada que contiene el momento; si se cruzan dos, la de entrada más cercana."""
    candidates = [o for o in occurrences if o.contains(moment)]
    return min(candidates, key=lambda o: abs((o.start - moment).total_seconds()), default=None)


def in_working_hours(start: datetime, end: datetime, moment: datetime) -> bool:
    """Dentro del horario programado de la jornada: desde la entrada y antes de la salida. Es cuando se
    puede tomar un descanso (a la hora que el empleado quiera), no en el margen para checar antes de
    la entrada ni después de la salida."""
    return start <= moment < end


def resolve_clock(occurrence: Occurrence, clock: time, zone: ZoneInfo) -> datetime:
    """La hora "HH:MM" (hora del negocio) de un registro de esa jornada como fecha y hora: del día de la
    jornada, o del siguiente si así queda más de 12 h antes de la entrada (la salida de un turno
    nocturno, o una salida tardía después de medianoche)."""
    moment = datetime.combine(occurrence.work_date, clock, zone)
    return moment + timedelta(days=1) if moment < occurrence.start - timedelta(hours=12) else moment
