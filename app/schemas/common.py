from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel

from app.core.responses import ApiResponse, ErrorItem

#: Respuesta de error documentada en Swagger (success=false, data=null, errors=[...]).
ErrorResponse = ApiResponse[Any]


@dataclass(frozen=True)
class PageParams:
    """Página pedida por el cliente (`page` desde 1, `size` elementos). La valida `Pagination`."""

    page: int
    size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class Page[ItemT](BaseModel):
    """Contrato único de todo listado paginado: los elementos de la página, el total sin paginar y
    la página/tamaño con que se pidió (el cliente calcula el número de páginas)."""

    items: list[ItemT]
    total: int
    page: int
    size: int

    @classmethod
    def of(cls, items: Sequence[ItemT], total: int, params: PageParams, **extra: Any) -> Self:
        """`extra`: campos propios de un listado (p. ej. los conteos del tablero de asistencia)."""
        return cls(items=list(items), total=total, page=params.page, size=params.size, **extra)


class Deletion(BaseModel):
    """Borrado lógico (`app/core/soft_delete.py`): cuándo y quién lo mandó a «Eliminados» (nulos si está vigente).
    Lo llevan los registros que se pueden eliminar y restaurar; la papelera los muestra con esa marca."""

    #: Cuándo se eliminó (UTC); la depuración lo borra de verdad `SOFT_DELETE_RETENTION_DAYS` después.
    deleted_at: datetime | None = None
    #: Correo de quien lo eliminó (literal: sobrevive a que esa cuenta se elimine).
    deleted_by: str | None = None


def deletion_of(record: Any) -> dict[str, Any]:
    """Los campos de `Deletion` de un registro con borrado lógico (para armar su esquema de lectura)."""
    return {"deleted_at": record.deleted_at, "deleted_by": record.deleted_by}


class EmployeeRef(BaseModel):
    """Empleado resumido (turnos, asistencia, calendario y operaciones masivas)."""

    id: int
    full_name: str
    employee_number: str
    #: En «Eliminados»: el historial lo sigue nombrando con la marca «Eliminado».
    deleted: bool = False


__all__ = ["ApiResponse", "Deletion", "EmployeeRef", "ErrorItem", "ErrorResponse", "Page", "PageParams", "deletion_of"]
