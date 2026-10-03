from collections.abc import Sequence
from dataclasses import dataclass
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
    def of(cls, items: Sequence[ItemT], total: int, params: PageParams) -> Self:
        return cls(items=list(items), total=total, page=params.page, size=params.size)


__all__ = ["ApiResponse", "ErrorItem", "ErrorResponse", "Page", "PageParams"]
