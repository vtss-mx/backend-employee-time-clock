"""Domicilio con su punto en el mapa (hoy lo usan los validadores; reutilizable por otros registros)."""

import re
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.services.catalog_service import get_catalogs

#: Código postal por país (México: 5 dígitos). Los demás: 3 a 10 letras, dígitos, espacios o guiones.
_POSTAL_CODES = {"MX": (re.compile(r"^\d{5}$"), "El código postal de México tiene 5 dígitos")}
_POSTAL_CODE_ANY = re.compile(r"^[A-Z0-9][A-Z0-9 -]{1,8}[A-Z0-9]$")


def _required(label: str, min_length: int = 2) -> AfterValidator:
    """Texto obligatorio sin espacios de sobra."""

    def clean(value: str) -> str:
        text = " ".join(value.split())
        if len(text) < min_length:
            raise ValueError(f"{label} es obligatorio" if not text else f"{label} es muy corto")
        return text

    return AfterValidator(clean)


def _optional(value: str | None) -> str | None:
    text = " ".join((value or "").split())
    return text or None


def _country(value: str) -> str:
    code = value.strip().upper()
    if not get_catalogs().is_active("countries", code):
        raise ValueError("Elige un país de la lista")
    return code


class Address(BaseModel):
    """Domicilio: calle, número exterior e interior, código postal, país, estado, municipio y ciudad.

    El punto en el mapa (latitud y longitud) es opcional: lo fija la empresa en el mapa o con la
    búsqueda de lugares, y es obligatorio cuando el registro exige ubicación.
    """

    street: Annotated[str, Field(max_length=150, description="Calle"), _required("La calle")]
    exterior_number: Annotated[
        str, Field(max_length=20, description="Número exterior (o S/N)"), _required("El número exterior", 1)
    ]
    interior_number: Annotated[
        str | None, Field(max_length=20, description="Número interior"), AfterValidator(_optional)
    ] = None
    country_code: Annotated[
        str,
        Field(min_length=2, max_length=2, description="País (ISO 3166-1 alfa-2, catalog.countries)", examples=["MX"]),
        AfterValidator(_country),
    ]
    postal_code: str = Field(max_length=10, description="Código postal (México: 5 dígitos)")
    state: Annotated[str, Field(max_length=100, description="Estado"), _required("El estado")]
    municipality: Annotated[str, Field(max_length=100, description="Municipio o alcaldía"), _required("El municipio")]
    city: Annotated[str, Field(max_length=100, description="Ciudad"), _required("La ciudad")]
    latitude: float | None = Field(default=None, ge=-90, le=90, description="Latitud WGS84")
    longitude: float | None = Field(default=None, ge=-180, le=180, description="Longitud WGS84")

    @field_validator("postal_code")
    @classmethod
    def _postal_code(cls, value: str, info: ValidationInfo) -> str:
        code = " ".join(value.split()).upper()
        pattern, message = _POSTAL_CODES.get(info.data.get("country_code", ""), (_POSTAL_CODE_ANY, None))
        if not pattern.match(code):
            raise ValueError(message or "El código postal no es válido")
        return code

    @model_validator(mode="after")
    def _point(self) -> Self:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Indica la latitud y la longitud del punto en el mapa")
        return self

    @property
    def has_point(self) -> bool:
        return self.latitude is not None
