from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import Page


def clean_name(value: str) -> str:
    """Sin espacios sobrantes: "  Recursos   Humanos " y "Recursos Humanos" son el mismo nombre."""
    return " ".join((value or "").split())


class DepartmentCreate(BaseModel):
    name: str = Field(
        min_length=1, max_length=100, description="Nombre único en la empresa (sin distinguir mayúsculas)"
    )
    description: str | None = Field(default=None, max_length=500, description="Para qué es el departamento (opcional)")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = clean_name(value)
        if not cleaned:
            raise ValueError("El nombre es obligatorio")
        return cleaned

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class DepartmentUpdate(DepartmentCreate):
    """Edición: mismo contrato que el alta (nombre obligatorio, descripción opcional)."""


class DepartmentAssignment(BaseModel):
    """Empleado de la empresa que se asigna al departamento o se nombra responsable."""

    employee_id: int = Field(gt=0)


class DepartmentPerson(BaseModel):
    """Responsable de un departamento (datos para mostrarlo, sin nada sensible)."""

    model_config = ConfigDict(from_attributes=True)

    employee_id: int
    full_name: str
    employee_number: str
    active: bool


class DepartmentRead(BaseModel):
    id: int
    name: str
    description: str | None = None
    #: Empleados asignados (activos e inactivos).
    employee_count: int
    managers: list[DepartmentPerson]
    created_at: datetime
    updated_at: datetime


class DepartmentList(Page[DepartmentRead]):
    """Página de departamentos de la empresa (orden alfabético)."""
