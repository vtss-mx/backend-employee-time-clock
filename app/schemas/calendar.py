"""Contrato del calendario de días libres: festivos, ausencias (vacaciones, permisos, incapacidades) y
días laborables especiales (COMPANY), y las vacaciones o permisos que pide el empleado (EMPLOYEE).

Las fechas son días del calendario en la hora del negocio (`YYYY-MM-DD`); un rango incluye ambos
extremos.
"""

from datetime import date, datetime
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, Field, model_validator

from app.models.calendar import ABSENCE_MAX_DAYS
from app.schemas.bulk import EmployeeIds
from app.schemas.common import EmployeeRef, Page
from app.schemas.shift import Name
from app.services.calendar_rules import span_days


def _note(value: str | None) -> str | None:
    """Espacios de más fuera; una nota vacía no se guarda."""
    text = " ".join((value or "").split())
    return text or None


#: Nota opcional (lo que explica quien registra o pide).
Note = Annotated[str | None, Field(max_length=500), AfterValidator(_note)]


class DayOffRead(BaseModel):
    """Por qué un día no se trabaja (lo calcula el servidor)."""

    #: HOLIDAY (festivo) o el tipo de ausencia (catalog.day_off_types: VACATION, PERMISSION...).
    kind: str
    #: Nombre del festivo ("Navidad") o del tipo de ausencia ("Vacaciones").
    name: str
    #: El día al que aplica (el de la jornada: un turno nocturno es del día en que entra).
    work_date: date
    #: El rango completo (un festivo dura un día).
    starts_on: date
    ends_on: date


# ---------------------------------------------------------------- festivos


class HolidayCreate(BaseModel):
    holiday_date: date
    name: Annotated[Name, Field(max_length=120, examples=["Día de la empresa"])]


class HolidayRead(BaseModel):
    id: int
    holiday_date: date
    name: str
    #: Oficial (Ley Federal del Trabajo, art. 74) o propio de la empresa.
    official: bool
    created_at: datetime


class HolidayList(Page[HolidayRead]):
    """Festivos de la empresa en orden de fecha."""


class OfficialHolidaysResult(BaseModel):
    """Lo que agregó "Agregar festivos oficiales del año" (solo los que faltaban)."""

    year: int
    added: list[HolidayRead]
    #: Días oficiales que ya estaban (con ese u otro nombre): no se duplican.
    existing: int


# ---------------------------------------------------------------- ausencias


class _AbsenceDates(BaseModel):
    """Tipo y rango de días (ambos incluidos, hasta un año)."""

    type: str = Field(min_length=1, max_length=30, description="Tipo de ausencia (catalog.day_off_types)")
    starts_on: date
    ends_on: date
    note: Note = None

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.ends_on < self.starts_on:
            raise ValueError("La fecha final no puede ser anterior a la inicial")
        if span_days(self.starts_on, self.ends_on) > ABSENCE_MAX_DAYS:
            raise ValueError(f"Una ausencia dura a lo más {ABSENCE_MAX_DAYS} días")
        return self


class AbsenceCreate(_AbsenceDates):
    """La empresa registra una ausencia (aprobada de una vez) para uno o varios empleados: p. ej. las
    vacaciones colectivas de fin de año."""

    employee_ids: EmployeeIds


class AbsenceRequest(_AbsenceDates):
    """El empleado pide vacaciones o un permiso (un tipo que se puede pedir); queda pendiente."""


class AbsenceReject(BaseModel):
    note: str = Field(min_length=5, max_length=500, description="Motivo (lo ve el empleado)")


class AbsenceRead(BaseModel):
    id: int
    employee: EmployeeRef
    type: str
    starts_on: date
    ends_on: date
    #: Días del rango (ambos incluidos).
    days: int
    note: str | None = None
    #: catalog.shift_request_statuses: PENDING, APPROVED, REJECTED o CANCELLED.
    status: str
    #: La pidió el propio empleado (si no, la registró la empresa).
    requested_by_employee: bool
    decided_at: datetime | None = None
    decision_note: str | None = None
    created_at: datetime


class AbsenceList(Page[AbsenceRead]):
    """Ausencias (la de fecha más reciente primero)."""


class AbsenceSummary(BaseModel):
    """Solicitudes de vacaciones o permisos por decidir (contador del menú)."""

    pending: int


# ---------------------------------------------------------------- días laborables especiales


class WorkdayCreate(BaseModel):
    """El empleado sí trabaja ese día aunque sea festivo o esté dentro de su ausencia."""

    employee_id: int = Field(gt=0)
    work_date: date
    note: Annotated[str | None, Field(max_length=300), AfterValidator(_note)] = None


class WorkdayRead(BaseModel):
    id: int
    employee: EmployeeRef
    work_date: date
    note: str | None = None
    created_at: datetime


class WorkdayList(Page[WorkdayRead]):
    """Días laborables especiales (los más recientes primero)."""
