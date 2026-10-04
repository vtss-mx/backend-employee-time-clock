"""Reglas de una jornada que registra o corrige la empresa (funciones puras, sin BD).

Son las mismas que respeta el registro en vivo, aplicadas a horas que la empresa declara:
- La entrada, dentro de la ventana para checarla (desde `early_check_in_minutes` antes de la entrada
  y antes de la salida programada); la salida, después de la entrada y hasta el límite para checarla.
- Nada en el futuro (la hora del servidor manda).
- Descansos: a lo más los que permite el turno (como en vivo: no se puede iniciar uno más), cada uno
  dentro del horario programado (de la entrada a la salida programadas), después de la entrada,
  antes de la salida (o de ahora, si la jornada sigue abierta) y sin encimarse. Un descanso más largo
  de lo permitido se acepta y su exceso queda a la vista (`exceeded_minutes`), igual que en vivo.
"""

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.exceptions import UnprocessableError
from app.services.shift_rules import Occurrence, in_working_hours


@dataclass(frozen=True)
class RecordTimes:
    """Las horas declaradas de una jornada (ya resueltas a fecha y hora)."""

    check_in: datetime
    check_out: datetime | None = None
    breaks: list[tuple[datetime, datetime]] = field(default_factory=list)


def _invalid(message: str, code: str, field_name: str) -> UnprocessableError:
    return UnprocessableError(message, code=code, field=field_name)


def _clock(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%H:%M")


def _check_in_problem(times: RecordTimes, occurrence: Occurrence, now: datetime) -> str | None:
    if times.check_in > now:
        return "TIME_IN_FUTURE"
    if not occurrence.opens <= times.check_in < occurrence.end:
        return "CHECK_IN_OUTSIDE_SHIFT"
    return None


def _check_out_problem(times: RecordTimes, occurrence: Occurrence, now: datetime) -> str | None:
    if times.check_out is None:
        return None
    if times.check_out > now:
        return "TIME_IN_FUTURE"
    if times.check_out <= times.check_in:
        return "CHECK_OUT_BEFORE_CHECK_IN"
    if times.check_out > occurrence.deadline:
        return "CHECK_OUT_AFTER_DEADLINE"
    return None


def _breaks_problem(times: RecordTimes, occurrence: Occurrence, now: datetime, allowed: int) -> str | None:
    if len(times.breaks) > allowed:
        return "BREAKS_EXCEEDED"
    limit = times.check_out or now
    previous_end: datetime | None = None
    for start, end in sorted(times.breaks):
        if end <= start:
            return "BREAK_INVALID"
        if start < times.check_in or end > limit:
            return "BREAK_OUTSIDE_SESSION"
        if not in_working_hours(occurrence.start, occurrence.end, start):
            return "BREAK_OUTSIDE_WORKING_HOURS"
        if previous_end is not None and start < previous_end:
            return "BREAKS_OVERLAP"
        previous_end = end
    return None


def record_problem(
    times: RecordTimes, occurrence: Occurrence, now: datetime, *, breaks_allowed: int, zone: ZoneInfo
) -> UnprocessableError | None:
    """El primer problema de las horas declaradas (con su campo), o None si se pueden registrar."""
    opens, end, deadline = (_clock(m, zone) for m in (occurrence.opens, occurrence.end, occurrence.deadline))
    start = _clock(occurrence.start, zone)
    messages = {
        "TIME_IN_FUTURE": ("Las horas no pueden ser posteriores a este momento", "check_in"),
        "CHECK_IN_OUTSIDE_SHIFT": (f"La entrada se registra entre las {opens} y las {end}", "check_in"),
        "CHECK_OUT_BEFORE_CHECK_IN": ("La salida debe ser posterior a la entrada", "check_out"),
        "CHECK_OUT_AFTER_DEADLINE": (f"La salida se registra a más tardar a las {deadline}", "check_out"),
        "BREAKS_EXCEEDED": (f"El turno permite {breaks_allowed} descanso(s)", "breaks"),
        "BREAK_INVALID": ("Cada descanso termina después de empezar", "breaks"),
        "BREAK_OUTSIDE_SESSION": ("Los descansos van entre la entrada y la salida", "breaks"),
        "BREAK_OUTSIDE_WORKING_HOURS": (f"Los descansos empiezan entre las {start} y las {end}", "breaks"),
        "BREAKS_OVERLAP": ("Los descansos no se pueden encimar", "breaks"),
    }
    code = (
        _check_in_problem(times, occurrence, now)
        or _check_out_problem(times, occurrence, now)
        or _breaks_problem(times, occurrence, now, breaks_allowed)
    )
    if code is None:
        return None
    message, field_name = messages[code]
    if code == "TIME_IN_FUTURE" and times.check_in <= now:
        field_name = "check_out"
    return _invalid(message, code, field_name)
