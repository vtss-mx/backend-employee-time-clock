"""Reglas puras del calendario, de la ventana de descansos y de la jornada que registra la empresa."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.services.attendance_rules import RecordTimes, record_problem
from app.services.calendar_rules import (
    HOLIDAY,
    DayOff,
    DaysOff,
    day_off_on,
    nth_weekday,
    official_holidays,
    overlaps,
    span_days,
)
from app.services.shift_rules import Occurrence, in_working_hours, occurrence_of, resolve_clock

ZONE = ZoneInfo(settings.APP_TIMEZONE)


# ---------------------------------------------------------------- festivos oficiales


def test_nth_weekday_of_a_month():
    assert nth_weekday(2026, 2, 0, 1) == date(2026, 2, 2)  # primer lunes de febrero
    assert nth_weekday(2026, 3, 0, 3) == date(2026, 3, 16)  # tercer lunes de marzo
    assert nth_weekday(2026, 11, 0, 3) == date(2026, 11, 16)
    assert nth_weekday(2027, 3, 0, 1) == date(2027, 3, 1)  # el mes empieza en lunes


def test_official_holidays_of_a_normal_year():
    assert official_holidays(2026) == [
        (date(2026, 1, 1), "Año Nuevo"),
        (date(2026, 2, 2), "Día de la Constitución"),
        (date(2026, 3, 16), "Natalicio de Benito Juárez"),
        (date(2026, 5, 1), "Día del Trabajo"),
        (date(2026, 9, 16), "Día de la Independencia"),
        (date(2026, 11, 16), "Día de la Revolución"),
        (date(2026, 12, 25), "Navidad"),
    ]


@pytest.mark.parametrize(
    ("year", "handover"),
    [(2024, date(2024, 10, 1)), (2030, date(2030, 10, 1)), (2036, date(2036, 10, 1)), (2018, date(2018, 12, 1))],
)
def test_the_transmission_of_the_executive_power_every_six_years(year, handover):
    days = dict(official_holidays(year))
    assert days[handover] == "Transmisión del Poder Ejecutivo Federal" and len(days) == 8
    assert [day for day, _ in official_holidays(year)] == sorted(days)


def test_no_transmission_in_other_years():
    assert all(name != "Transmisión del Poder Ejecutivo Federal" for _, name in official_holidays(2027))


# ---------------------------------------------------------------- rangos y días libres


def test_ranges_and_overlaps():
    assert span_days(date(2026, 12, 1), date(2026, 12, 1)) == 1
    assert span_days(date(2026, 12, 1), date(2026, 12, 15)) == 15
    first = (date(2026, 12, 1), date(2026, 12, 10))
    assert overlaps(*first, date(2026, 12, 10), date(2026, 12, 20))  # comparten el día 10
    assert overlaps(*first, date(2026, 11, 1), date(2026, 12, 31))  # lo contiene
    assert not overlaps(*first, date(2026, 12, 11), date(2026, 12, 20))
    assert not overlaps(*first, date(2026, 11, 1), date(2026, 11, 30))


def test_the_reason_told_to_the_employee():
    christmas = DayOff(HOLIDAY, "Navidad", date(2026, 12, 25), date(2026, 12, 25))
    assert christmas.explain(date(2026, 12, 25), date(2026, 12, 25)) == "Hoy es día festivo: Navidad"
    assert christmas.explain(date(2026, 12, 25), date(2026, 12, 24)) == "El 25/12/2026 es día festivo: Navidad"
    vacation = DayOff("VACATION", "Vacaciones", date(2026, 12, 1), date(2026, 12, 15), "Estás de vacaciones")
    assert vacation.explain(date(2026, 12, 3), date(2026, 12, 3)) == "Estás de vacaciones del 01/12/2026 al 15/12/2026"
    permission = DayOff("PERMISSION", "Permiso", date(2026, 12, 3), date(2026, 12, 3), "Tienes permiso")
    assert permission.explain(date(2026, 12, 3), date(2026, 12, 3)) == "Tienes permiso el 03/12/2026"


def test_a_day_is_off_by_holiday_or_approved_absence_unless_it_is_a_workday():
    vacation = DayOff("VACATION", "Vacaciones", date(2026, 12, 1), date(2026, 12, 15), "Estás de vacaciones")
    holidays = {date(2026, 12, 25): "Navidad"}
    days = DaysOff(
        holidays=holidays, absences={7: [vacation]}, workdays={(7, date(2026, 12, 2)), (8, date(2026, 12, 25))}
    )
    assert days.on(7, date(2026, 12, 1)) == vacation
    assert days.on(7, date(2026, 12, 2)) is None  # la empresa lo necesita ese día
    assert days.on(7, date(2026, 12, 16)) is None
    assert days.on(9, date(2026, 12, 25)) == DayOff(HOLIDAY, "Navidad", date(2026, 12, 25), date(2026, 12, 25))
    assert days.on(8, date(2026, 12, 25)) is None  # trabaja el festivo
    assert day_off_on(date(2026, 12, 5), {}, [], 9, frozenset()) is None
    assert DaysOff().on(1, date(2026, 1, 1)) is None


# ---------------------------------------------------------------- descansos y horas declaradas


class Morning:
    start_time = time(8)
    end_time = time(16)
    early_check_in_minutes = 15
    late_check_out_minutes = 60


class Night(Morning):
    start_time = time(22)
    end_time = time(6)


def test_breaks_only_within_working_hours():
    occurrence = occurrence_of(Morning(), date(2026, 3, 2), ZONE)  # type: ignore[arg-type]
    start, end = occurrence.start, occurrence.end
    assert in_working_hours(start, end, start)
    assert in_working_hours(start, end, end - timedelta(minutes=1))
    assert not in_working_hours(start, end, start - timedelta(minutes=1))  # margen para entrar antes
    assert not in_working_hours(start, end, end)  # ya es la salida


def test_declared_clock_times_belong_to_the_occurrence():
    day = date(2026, 3, 2)
    morning = occurrence_of(Morning(), day, ZONE)  # type: ignore[arg-type]
    assert resolve_clock(morning, time(7, 50), ZONE) == datetime(2026, 3, 2, 7, 50, tzinfo=ZONE)
    assert resolve_clock(morning, time(16, 30), ZONE) == datetime(2026, 3, 2, 16, 30, tzinfo=ZONE)
    night = occurrence_of(Night(), day, ZONE)  # type: ignore[arg-type]
    assert resolve_clock(night, time(21, 50), ZONE) == datetime(2026, 3, 2, 21, 50, tzinfo=ZONE)
    assert resolve_clock(night, time(5, 55), ZONE) == datetime(2026, 3, 3, 5, 55, tzinfo=ZONE)  # al día siguiente


def _at(hhmm: str, day: date = date(2026, 3, 2)) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE).astimezone(UTC)


@pytest.mark.parametrize(
    ("times", "code", "field"),
    [
        (RecordTimes(_at("07:50"), _at("16:00")), None, None),
        (RecordTimes(_at("07:00")), "CHECK_IN_OUTSIDE_SHIFT", "check_in"),
        (RecordTimes(_at("16:00")), "CHECK_IN_OUTSIDE_SHIFT", "check_in"),
        (RecordTimes(_at("08:00"), _at("19:00")), "TIME_IN_FUTURE", "check_out"),
        (RecordTimes(_at("08:00"), _at("08:00")), "CHECK_OUT_BEFORE_CHECK_IN", "check_out"),
        (RecordTimes(_at("08:00"), _at("17:01")), "CHECK_OUT_AFTER_DEADLINE", "check_out"),
        (
            RecordTimes(_at("08:00"), _at("16:00"), [(_at("10:00"), _at("10:10")), (_at("12:00"), _at("12:10"))]),
            "BREAKS_EXCEEDED",
            "breaks",
        ),
        (RecordTimes(_at("08:00"), _at("16:00"), [(_at("12:00"), _at("12:00"))]), "BREAK_INVALID", "breaks"),
        (RecordTimes(_at("09:00"), _at("16:00"), [(_at("08:30"), _at("09:30"))]), "BREAK_OUTSIDE_SESSION", "breaks"),
        (RecordTimes(_at("08:00"), _at("15:00"), [(_at("14:50"), _at("15:10"))]), "BREAK_OUTSIDE_SESSION", "breaks"),
        (RecordTimes(_at("08:00")), None, None),  # sin salida: los descansos hasta ahora
        (RecordTimes(_at("08:00"), None, [(_at("15:30"), _at("18:10"))]), "BREAK_OUTSIDE_SESSION", "breaks"),
        (
            RecordTimes(_at("07:50"), _at("17:00"), [(_at("07:55"), _at("08:05"))]),
            "BREAK_OUTSIDE_WORKING_HOURS",
            "breaks",
        ),
        (
            RecordTimes(_at("07:50"), _at("17:00"), [(_at("16:00"), _at("16:30"))]),
            "BREAK_OUTSIDE_WORKING_HOURS",
            "breaks",
        ),
    ],
)
def test_declared_times_follow_the_live_rules(times, code, field):
    occurrence = occurrence_of(Morning(), date(2026, 3, 2), ZONE)  # type: ignore[arg-type]
    problem = record_problem(times, occurrence, _at("18:00"), breaks_allowed=1, zone=ZONE)
    assert (problem.code, problem.field) == (code, field) if code else problem is None


def test_breaks_cannot_overlap_and_the_check_in_cannot_be_in_the_future():
    occurrence = occurrence_of(Morning(), date(2026, 3, 2), ZONE)  # type: ignore[arg-type]
    two = RecordTimes(_at("08:00"), _at("16:00"), [(_at("10:00"), _at("10:30")), (_at("10:20"), _at("10:40"))])
    overlap = record_problem(two, occurrence, _at("18:00"), breaks_allowed=2, zone=ZONE)
    assert overlap is not None and overlap.code == "BREAKS_OVERLAP"
    fine = RecordTimes(_at("08:00"), _at("16:00"), [(_at("10:00"), _at("10:30")), (_at("10:30"), _at("10:40"))])
    assert record_problem(fine, occurrence, _at("18:00"), breaks_allowed=2, zone=ZONE) is None
    future = record_problem(RecordTimes(_at("09:00")), occurrence, _at("08:30"), breaks_allowed=1, zone=ZONE)
    assert future is not None and (future.code, future.field) == ("TIME_IN_FUTURE", "check_in")
    assert future.message == "Las horas no pueden ser posteriores a este momento"
    assert isinstance(occurrence, Occurrence)
