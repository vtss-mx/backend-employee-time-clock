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
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.core.exceptions import UnprocessableError
from app.services.shift_rules import Occurrence, in_working_hours


@dataclass(frozen=True)
class RecordTimes:
    """Las horas declaradas de una jornada (ya resueltas a fecha y hora)."""

    check_in: datetime
    check_out: datetime | None = None
    breaks: list[tuple[datetime, datetime]] = field(default_factory=list)


def _clock(moment: datetime, zone: ZoneInfo) -> time:
    """La hora en la zona del negocio (el mensaje la escribe como la acostumbra cada idioma)."""
    return moment.astimezone(zone).time()


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
    """El primer problema de las horas declaradas (con su campo), o None si se pueden registrar. El código es
    también la llave de su mensaje; los datos van como parámetros."""
    opens, end, deadline = (_clock(m, zone) for m in (occurrence.opens, occurrence.end, occurrence.deadline))
    start = _clock(occurrence.start, zone)
    problems: dict[str, tuple[dict[str, object] | None, str]] = {
        "TIME_IN_FUTURE": (None, "check_in"),
        "CHECK_IN_OUTSIDE_SHIFT": ({"opens": opens, "end": end}, "check_in"),
        "CHECK_OUT_BEFORE_CHECK_IN": (None, "check_out"),
        "CHECK_OUT_AFTER_DEADLINE": ({"deadline": deadline}, "check_out"),
        "BREAKS_EXCEEDED": ({"count": breaks_allowed}, "breaks"),
        "BREAK_INVALID": (None, "breaks"),
        "BREAK_OUTSIDE_SESSION": (None, "breaks"),
        "BREAK_OUTSIDE_WORKING_HOURS": ({"start": start, "end": end}, "breaks"),
        "BREAKS_OVERLAP": (None, "breaks"),
    }
    code = (
        _check_in_problem(times, occurrence, now)
        or _check_out_problem(times, occurrence, now)
        or _breaks_problem(times, occurrence, now, breaks_allowed)
    )
    if code is None:
        return None
    params, field_name = problems[code]
    if code == "TIME_IN_FUTURE" and times.check_in <= now:
        field_name = "check_out"
    return UnprocessableError(code=code, params=params, field=field_name)
