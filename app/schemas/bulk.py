"""Operaciones para varios empleados a la vez (asignar un turno, registrar vacaciones colectivas).

Una sola petición, una sola transacción y un resultado por empleado: hecho, sin cambios (ya tenía
exactamente eso: un reintento de la red no duplica ni falla) u omitido con su código y su motivo
(p. ej. inactivo o con un cambio ya programado). Lo que no depende de cada empleado (el turno, las
fechas, los sitios) se valida antes y rechaza toda la petición.
"""

from collections.abc import Iterable
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, Field

from app.core.exceptions import AppError
from app.i18n import LocalizedValueError
from app.schemas.common import EmployeeRef

#: Empleados por operación: lo que cabe en una pantalla de selección y en una transacción corta.
BULK_MAX = 500

BulkResultCode = Literal["DONE", "UNCHANGED", "SKIPPED"]


def _unique(ids: list[int]) -> list[int]:
    if len(set(ids)) != len(ids):
        raise LocalizedValueError("EMPLOYEE_IDS_REPEATED")
    return ids


#: Ids de empleados de la empresa (1 a BULK_MAX, sin repetir).
EmployeeIds = Annotated[
    list[Annotated[int, Field(gt=0)]], Field(min_length=1, max_length=BULK_MAX), AfterValidator(_unique)
]


class BulkOutcome(BaseModel):
    """Lo que pasó con un empleado: DONE (hecho), UNCHANGED (ya lo tenía) o SKIPPED (con su motivo)."""

    employee: EmployeeRef
    result: BulkResultCode
    #: Código estable del motivo (EMPLOYEE_INACTIVE, ASSIGNMENT_ALREADY_SCHEDULED...) si se omitió.
    code: str | None = None
    message: str | None = None

    @classmethod
    def of(cls, employee: EmployeeRef, result: BulkResultCode, reason: AppError | None = None) -> Self:
        """El resultado de un empleado; si se omitió, con el código y el mensaje de su motivo."""
        return cls(
            employee=employee,
            result=result,
            code=reason.code if reason else None,
            message=reason.message if reason else None,
        )


class BulkResult(BaseModel):
    """Cuántos se hicieron, cuántos ya estaban y cuántos se omitieron, con el detalle de cada uno."""

    done: int
    unchanged: int
    skipped: int
    results: list[BulkOutcome]

    @classmethod
    def of(cls, outcomes: Iterable[BulkOutcome]) -> Self:
        results = list(outcomes)
        count = {code: sum(1 for o in results if o.result == code) for code in ("DONE", "UNCHANGED", "SKIPPED")}
        return cls(done=count["DONE"], unchanged=count["UNCHANGED"], skipped=count["SKIPPED"], results=results)
