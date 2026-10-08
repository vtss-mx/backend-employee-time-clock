from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ModelWrapValidatorHandler, PrivateAttr, model_validator

from app.core.responses import ApiResponse, ErrorItem
from app.i18n import LazyText, render_text

#: Respuesta de error documentada en Swagger (success=false, data=null, errors=[...]).
ErrorResponse = ApiResponse[Any]


class Explained(BaseModel):
    """Un resultado que le explica algo a la persona en `message` (regla 16: nunca se mezclan idiomas).

    Se construye con el texto DIFERIDO (`message=Text("LLAVE", ...)` o una función que lo arma, p. ej.
    `reason_text(code)`): `message` queda armado en el idioma de la petición (lo que viaja en `data`) y `text`
    conserva el diferido para el sobre (`ok(result, result.text)`), que lo arma en cada idioma (`i18n`): así la
    aplicación web cambia de idioma el aviso del resultado sin repetir la petición. Un resultado leído de un JSON (la
    validación de la respuesta) trae `message` ya armado y su `text` es ese mismo texto."""

    message: str
    _text: LazyText | None = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def _keep_text(cls, values: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        lazy = values.get("message") if isinstance(values, dict) else None
        if lazy is None or isinstance(lazy, str):
            return handler(values)
        model = handler({**values, "message": render_text(lazy)})
        model._text = lazy
        return model

    @property
    def text(self) -> LazyText:
        """El texto diferido de `message` (el que se construyó; uno ya armado, tal cual)."""
        if self._text is not None:
            return self._text
        message = self.message
        return lambda: message


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
    #: Opcional (migración 0076): null si el empleado no tiene número.
    employee_number: str | None = None
    #: En «Eliminados»: el historial lo sigue nombrando con la marca «Eliminado».
    deleted: bool = False
    #: Ruta versionada de su foto de perfil (`/users/{id}/avatar?v=...`) o None (sin foto o en «Eliminados»): la app
    #: la dibuja con `Avatar` y, sin ella, las iniciales. La integración no la recibe (sus esquemas son otros).
    avatar: str | None = None


__all__ = [
    "ApiResponse",
    "Deletion",
    "EmployeeRef",
    "ErrorItem",
    "ErrorResponse",
    "Explained",
    "Page",
    "PageParams",
    "deletion_of",
]
