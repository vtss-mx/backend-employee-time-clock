"""Contrato de la asistencia por turno: lo que ve el empleado hoy, cada registro y el tablero de la empresa.

Las horas son de la hora del servidor (UTC en la API); la app las muestra en la hora del negocio.
"""

from datetime import date, datetime, time
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from app.models.shift import MAX_BREAKS
from app.schemas.calendar import DayOffRead
from app.schemas.common import EmployeeRef, Page
from app.schemas.shift import ShiftRef, SiteRef
from app.schemas.verification import VerificationResult


class BreakRead(BaseModel):
    started_at: datetime
    ended_at: datetime | None = None
    minutes: int
    exceeded_minutes: int


class WorkSessionRead(BaseModel):
    """La jornada de un turno (con lo programado al momento de entrar)."""

    id: int
    work_date: date
    shift_name: str
    scheduled_start: datetime
    scheduled_end: datetime
    check_out_deadline: datetime
    status: str
    check_in_at: datetime
    check_in_mode: str
    check_in_site: str | None = None
    check_out_at: datetime | None = None
    check_out_mode: str | None = None
    check_out_site: str | None = None
    late_minutes: int
    early_leave_minutes: int
    break_minutes: int
    worked_minutes: int | None = None
    breaks_allowed: int
    break_minutes_allowed: int
    breaks: list[BreakRead]
    #: La empresa la registró o la corrigió: cuándo y por qué (lo ve también el empleado).
    edited_at: datetime | None = None
    edit_reason: str | None = None


class OccurrenceRead(BaseModel):
    """Una jornada programada: su entrada, su salida y la ventana para checar."""

    work_date: date
    start: datetime
    end: datetime
    opens: datetime
    deadline: datetime


class BreakWindowRead(BaseModel):
    """Cuándo puede tomar sus descansos (lo decide el servidor): cuando quiera, pero dentro de su horario
    (de la entrada a la salida programadas) y mientras le queden."""

    starts_at: datetime
    ends_at: datetime
    #: Minutos de cada descanso y cuántos le quedan.
    minutes: int
    remaining: int


class AttendanceToday(BaseModel):
    """Qué puede hacer el empleado ahora y por qué (todo lo decide el servidor)."""

    #: Hora del servidor (para mostrar cuánto falta sin confiar en el reloj del teléfono).
    now: datetime
    shift: ShiftRef | None = None
    #: La jornada en curso (o la que se puede checar ahora).
    occurrence: OccurrenceRead | None = None
    #: La siguiente jornada si ahora no hay ninguna.
    next_occurrence: OccurrenceRead | None = None
    session: WorkSessionRead | None = None
    #: Acciones permitidas ahora (CHECK_IN, BREAK_START, BREAK_END, CHECK_OUT).
    actions: list[str]
    #: Hoy puede checar remoto; si no, solo en sus sitios.
    remote_allowed: bool = False
    sites: list[SiteRef]
    message: str
    #: Hoy (o los próximos días) no trabaja: festivo o ausencia aprobada; no se puede checar la entrada.
    day_off: DayOffRead | None = None
    #: Con jornada abierta: la ventana de sus descansos.
    break_window: BreakWindowRead | None = None


class AttendanceActionResult(BaseModel):
    """Resultado de un registro: primero la verificación facial; si pasó, lo registrado."""

    verified: bool
    message: str
    action: str
    verification: VerificationResult
    session: WorkSessionRead | None = None


class AttendanceHistory(Page[WorkSessionRead]):
    """Jornadas del empleado (la más reciente primero)."""


class BoardRow(BaseModel):
    """Un empleado en el tablero del día: su turno y en qué va."""

    employee: EmployeeRef
    department: str | None = None
    shift_name: str
    scheduled_start: datetime
    scheduled_end: datetime
    #: SCHEDULED (aún no es hora), MISSING (debió entrar y no ha checado), WORKING, ON_BREAK, DONE,
    #: MISSED_CHECKOUT (no checó su salida), ABSENT (terminó su turno sin entrada) o DAY_OFF (no trabaja
    #: ese día: festivo o ausencia aprobada; no es una falta).
    state: str
    session: WorkSessionRead | None = None
    #: Por qué no trabaja ese día (con DAY_OFF).
    day_off: DayOffRead | None = None


class AttendanceBoard(Page[BoardRow]):
    """El tablero de un día: los empleados con turno ese día."""

    work_date: date
    #: Jornadas de ese día: en turno, en descanso, completas y sin salida; y quienes tienen día libre.
    working: int
    on_break: int
    done: int
    missed_checkout: int
    day_off: int = 0


class CompanySessionRead(WorkSessionRead):
    employee: EmployeeRef


class CompanySessionList(Page[CompanySessionRead]):
    """Historial de jornadas de la empresa (la más reciente primero)."""


class AttendanceEventRead(BaseModel):
    """Un registro de la bitácora (la evidencia de la jornada)."""

    action: str
    mode: str
    site: str | None = None
    occurred_at: datetime
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    distance_m: float | None = None
    #: Confianza de la verificación facial que lo respaldó.
    confidence: float | None = None
    #: Quién operó: el empleado, el validador (su nombre) o la empresa (su correo).
    operator: str | None = None
    #: Motivo de un registro de la empresa (modalidad COMPANY).
    note: str | None = None


class CompanySessionDetail(CompanySessionRead):
    events: list[AttendanceEventRead]


# ---------------------------------------------------------------- registro de la empresa


def _minute(value: time) -> time:
    """Las horas se registran al minuto."""
    return value.replace(second=0, microsecond=0)


def _reason(value: str) -> str:
    text = " ".join(value.split())
    if len(text) < 5:
        raise ValueError("Explica brevemente el motivo")
    return text


Clock = Annotated[time, AfterValidator(_minute)]


class BreakTimes(BaseModel):
    """Un descanso que tomó (hora del negocio, "HH:MM")."""

    start: Clock
    end: Clock


class ManualTimes(BaseModel):
    """Las horas de la jornada en la hora del negocio ("HH:MM"): las de un turno nocturno después de
    medianoche son del día siguiente. Sin salida, la jornada queda abierta (o sin salida si ya venció)."""

    check_in: Clock
    check_out: Clock | None = None
    breaks: list[BreakTimes] = Field(default_factory=list, max_length=MAX_BREAKS)
    #: Por qué la registra o la corrige la empresa (lo ve también el empleado).
    reason: Annotated[str, Field(min_length=5, max_length=500), AfterValidator(_reason)]


class ManualSessionCreate(ManualTimes):
    """La jornada de un empleado que no registró (el día de su turno)."""

    employee_id: int = Field(gt=0)
    work_date: date


class ManualSessionUpdate(ManualTimes):
    """Corregir una jornada: sus horas y descansos completos (lo anterior queda en la bitácora)."""
