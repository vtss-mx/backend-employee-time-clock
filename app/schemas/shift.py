"""Contrato de sitios de trabajo, turnos, asignaciones y solicitudes de cambio de turno (COMPANY y EMPLOYEE).

Los días de la semana viajan como lista de números (lunes = 0 ... domingo = 6); en la base son una
máscara de bits (`shift_rules.mask_of`). Las horas son de la hora del negocio.
"""

from datetime import date, datetime, time
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from app.models.shift import (
    BREAK_MINUTES_MAX,
    BREAK_MINUTES_MIN,
    CHECK_OUT_WINDOW_MAX,
    MAX_BREAKS,
    SITE_RADIUS_MAX_M,
    SITE_RADIUS_MIN_M,
    TOLERANCE_MAX,
)
from app.schemas.address import Address
from app.schemas.bulk import EmployeeIds
from app.schemas.common import EmployeeRef, Page
from app.schemas.department import clean_name
from app.services.shift_rules import MINUTES_PER_DAY, duration_minutes, window_minutes


def _weekdays(days: list[int]) -> list[int]:
    if any(day < 0 or day > 6 for day in days):
        raise ValueError("Los días van de 0 (lunes) a 6 (domingo)")
    return sorted(set(days))


#: Días de la semana: 0 = lunes ... 6 = domingo (sin repetir, en orden).
Weekdays = Annotated[list[int], Field(max_length=7, examples=[[0, 1, 2, 3, 4]]), AfterValidator(_weekdays)]


def _name(value: str) -> str:
    cleaned = clean_name(value)
    if len(cleaned) < 2:
        raise ValueError("El nombre es obligatorio")
    return cleaned


Name = Annotated[str, AfterValidator(_name)]


class _ActiveUpdate(BaseModel):
    active: bool


# ---------------------------------------------------------------- sitios de trabajo


class SiteCreate(BaseModel):
    """Sitio de trabajo: checar en sitio es hacerlo a no más de `radius_m` metros de su punto."""

    name: Annotated[Name, Field(max_length=120, examples=["Planta Hermosillo"])]
    address: Address = Field(description="Domicilio con su punto en el mapa (obligatorio)")
    radius_m: int = Field(ge=SITE_RADIUS_MIN_M, le=SITE_RADIUS_MAX_M, description="Radio de la geocerca (m)")

    @field_validator("address")
    @classmethod
    def _point(cls, address: Address) -> Address:
        if address.latitude is None:
            raise ValueError("Marca en el mapa el punto del sitio")
        return address


class SiteUpdate(SiteCreate):
    """Edición: el sitio completo (nombre, domicilio con su punto y radio)."""


class SiteStatusUpdate(_ActiveUpdate):
    pass


class SiteRead(BaseModel):
    id: int
    name: str
    address: Address
    radius_m: int
    active: bool
    #: Empleados que hoy pueden checar en este sitio (asignaciones vigentes).
    employees: int = 0
    created_at: datetime


class SiteList(Page[SiteRead]):
    """Sitios de la empresa (orden alfabético)."""


class SiteRef(BaseModel):
    """Sitio para mostrarlo y medir la distancia (punto y radio)."""

    id: int
    name: str
    latitude: float
    longitude: float
    radius_m: int


# ---------------------------------------------------------------- turnos


class _ShiftFields(BaseModel):
    start_time: time = Field(description="Hora de entrada (hora del negocio)", examples=["07:00"])
    end_time: time = Field(description="Hora de salida; igual o antes que la entrada = termina al día siguiente")
    weekdays: Weekdays = Field(min_length=1, description="Días en que empieza el turno (0 = lunes)")
    breaks_count: int = Field(default=0, ge=0, le=MAX_BREAKS, description="Descansos por turno")
    break_minutes: int = Field(default=0, ge=0, le=BREAK_MINUTES_MAX, description="Minutos de cada descanso")
    early_check_in_minutes: int = Field(default=15, ge=0, le=TOLERANCE_MAX, description="Checar antes de la entrada")
    late_tolerance_minutes: int = Field(default=10, ge=0, le=TOLERANCE_MAX, description="Retardo tolerado")
    early_check_out_minutes: int = Field(default=0, ge=0, le=TOLERANCE_MAX, description="Salida anticipada tolerada")
    late_check_out_minutes: int = Field(
        default=60, ge=0, le=CHECK_OUT_WINDOW_MAX, description="Límite para checar la salida después de la hora"
    )

    @model_validator(mode="after")
    def _times(self) -> Self:
        if self.start_time.replace(second=0, microsecond=0) == self.end_time.replace(second=0, microsecond=0):
            raise ValueError("La hora de salida debe ser distinta de la de entrada")
        if self.breaks_count and not self.break_minutes >= BREAK_MINUTES_MIN:
            raise ValueError(f"Cada descanso dura al menos {BREAK_MINUTES_MIN} minutos")
        if not self.breaks_count:
            self.break_minutes = 0
        self.start_time = self.start_time.replace(second=0, microsecond=0)
        self.end_time = self.end_time.replace(second=0, microsecond=0)
        if self.breaks_count * self.break_minutes >= duration_minutes(self):
            raise ValueError("Los descansos no pueden sumar todo el turno")
        # Dos jornadas seguidas del mismo turno no deben encimarse: la ventana completa (entrada
        # temprana + turno + salida tardía) dura menos de un día.
        if window_minutes(self) >= MINUTES_PER_DAY:
            raise ValueError("La entrada temprana, el turno y el límite de salida deben sumar menos de 24 horas")
        return self


class ShiftCreate(_ShiftFields):
    name: Annotated[Name, Field(max_length=80, examples=["Matutino"])]


class ShiftUpdate(ShiftCreate):
    """Edición: el turno completo. Aplica a las jornadas que aún no empiezan; lo registrado no cambia."""


class ShiftStatusUpdate(_ActiveUpdate):
    pass


class ShiftRef(BaseModel):
    """Turno resumido (para asignaciones, solicitudes y la asistencia)."""

    id: int
    name: str
    start_time: time
    end_time: time
    overnight: bool
    weekdays: list[int]


class ShiftRead(ShiftRef):
    breaks_count: int
    break_minutes: int
    early_check_in_minutes: int
    late_tolerance_minutes: int
    early_check_out_minutes: int
    late_check_out_minutes: int
    duration_minutes: int
    active: bool
    #: Empleados con el turno vigente hoy.
    employees: int = 0
    created_at: datetime


class ShiftList(Page[ShiftRead]):
    """Turnos de la empresa (orden alfabético)."""


# ---------------------------------------------------------------- asignaciones


class AssignmentCreate(BaseModel):
    """El turno de un empleado desde una fecha (desde mañana o después si ya tiene uno)."""

    shift_id: int = Field(gt=0)
    valid_from: date
    remote_weekdays: Weekdays = Field(default_factory=list, description="Días en que puede checar remoto")
    site_ids: list[int] = Field(
        default_factory=list,
        max_length=50,
        description="Sitios donde checa en persona (obligatorio si algún día no es remoto)",
    )


class AssignmentBulkCreate(AssignmentCreate):
    """El mismo turno, fecha, días remotos y sitios para varios empleados a la vez (con las mismas reglas
    que la asignación de uno: quien ya tiene turno lo cambia desde mañana o después)."""

    employee_ids: EmployeeIds


class AssignmentRead(BaseModel):
    id: int
    shift: ShiftRef
    valid_from: date
    valid_to: date | None = None
    remote_weekdays: list[int]
    sites: list[SiteRef]
    #: Vigente hoy, programada (empieza después) o terminada.
    state: str
    created_at: datetime


class AssignmentList(Page[AssignmentRead]):
    """Asignaciones del empleado (la más reciente primero)."""


# ---------------------------------------------------------------- solicitudes de cambio de turno


class ShiftRequestCreate(BaseModel):
    shift_id: int = Field(gt=0)
    valid_from: date = Field(description="Desde cuándo (al menos un día de anticipación)")
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        text = " ".join(value.split())
        if len(text) < 5:
            raise ValueError("Explica brevemente el motivo")
        return text


class ShiftRequestApprove(BaseModel):
    """Aprobar: puede ajustar la fecha (si la pedida ya no tiene un día de anticipación), los días
    remotos y los sitios; sin ellos, conserva los de la asignación actual que apliquen."""

    valid_from: date | None = None
    remote_weekdays: Weekdays | None = None
    site_ids: list[int] | None = Field(default=None, max_length=50)


class ShiftRequestReject(BaseModel):
    note: str = Field(min_length=5, max_length=500, description="Motivo (lo ve el empleado)")


class ShiftRequestRead(BaseModel):
    id: int
    employee: EmployeeRef
    shift: ShiftRef
    #: El turno que tenía al pedirlo (None si no tenía).
    current_shift: ShiftRef | None = None
    valid_from: date
    reason: str
    status: str
    review_note: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime


class ShiftRequestList(Page[ShiftRequestRead]):
    """Solicitudes (la más reciente primero)."""


class ShiftRequestSummary(BaseModel):
    pending: int
