"""Domicilio con su punto en el mapa (validadores y sitios de trabajo: columnas de `AddressMixin`).

Los campos van en el orden en que se capturan (decisión del dueño del producto): país, estado,
municipio o alcaldía, ciudad o localidad, colonia o barrio, código postal, calle, número exterior,
número interior y referencias. Así los lee también quien usa la API.
"""

import re
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.i18n import LocalizedValueError
from app.services.catalog_service import get_catalogs

#: Código postal por país (México: 5 dígitos) con la llave de su mensaje. Los demás: 3 a 10 letras, dígitos,
#: espacios o guiones.
_POSTAL_CODES = {"MX": (re.compile(r"^\d{5}$"), "POSTAL_CODE_MX")}
_POSTAL_CODE_ANY = re.compile(r"^[A-Z0-9][A-Z0-9 -]{1,8}[A-Z0-9]$")


def _required(missing: str, short: str | None = None, min_length: int = 2) -> AfterValidator:
    """Texto obligatorio sin espacios de sobra (también si llega nulo: dice qué falta, con el nombre del campo:
    «La colonia es obligatoria», «El estado es muy corto»). `missing` y `short` son las llaves de sus mensajes (cada
    idioma concuerda el género y el número a su manera)."""

    def clean(value: str | None) -> str:
        text = " ".join((value or "").split())
        if len(text) < min_length:
            raise LocalizedValueError(missing if not text or short is None else short)
        return text

    return AfterValidator(clean)


def _optional(value: str | None) -> str | None:
    text = " ".join((value or "").split())
    return text or None


def _notes(value: str | None) -> str | None:
    """Referencias: cada indicación en su renglón (se conservan los saltos de línea), sin espacios de
    sobra ni renglones vacíos; vacías → None."""
    lines = (" ".join(line.split()) for line in (value or "").splitlines())
    return "\n".join(line for line in lines if line) or None


def _country(value: str) -> str:
    code = value.strip().upper()
    if not get_catalogs().is_active("countries", code):
        raise LocalizedValueError("COUNTRY_INVALID")
    return code


class Address(BaseModel):
    """Domicilio: país, estado, municipio, ciudad, colonia, código postal, calle, números y referencias.

    El punto en el mapa (latitud y longitud) es opcional: lo fija la empresa en el mapa o con la
    búsqueda de lugares, y es obligatorio cuando el registro exige ubicación.

    La colonia (`neighborhood`) es obligatoria al guardar, pero los domicilios guardados antes de pedirla
    (migración 0049) la tienen nula: se leen igual (`address_of` no vuelve a validar) y la empresa la
    completa al editarlos. Por eso su tipo admite nulo y su valor por omisión se valida
    (`validate_default`): un cuerpo sin la colonia responde «La colonia es obligatoria», no un error
    genérico en inglés.
    """

    country_code: Annotated[
        str,
        Field(min_length=2, max_length=2, description="País (ISO 3166-1 alfa-2, catalog.countries)", examples=["MX"]),
        AfterValidator(_country),
    ]
    state: Annotated[
        str,
        Field(max_length=100, description="Estado o provincia"),
        _required("ADDRESS_STATE_REQUIRED", "ADDRESS_STATE_TOO_SHORT"),
    ]
    municipality: Annotated[
        str,
        Field(max_length=100, description="Municipio o alcaldía"),
        _required("ADDRESS_MUNICIPALITY_REQUIRED", "ADDRESS_MUNICIPALITY_TOO_SHORT"),
    ]
    city: Annotated[
        str,
        Field(max_length=100, description="Ciudad o localidad"),
        _required("ADDRESS_CITY_REQUIRED", "ADDRESS_CITY_TOO_SHORT"),
    ]
    neighborhood: Annotated[
        str | None,
        Field(
            validate_default=True,
            max_length=120,
            description="Colonia o barrio (obligatoria al guardar; nula en domicilios guardados antes de pedirla)",
            examples=["Centro"],
        ),
        _required("ADDRESS_NEIGHBORHOOD_REQUIRED", "ADDRESS_NEIGHBORHOOD_TOO_SHORT"),
    ] = None
    postal_code: str = Field(max_length=10, description="Código postal (México: 5 dígitos)")
    street: Annotated[
        str,
        Field(max_length=150, description="Calle o vialidad"),
        _required("ADDRESS_STREET_REQUIRED", "ADDRESS_STREET_TOO_SHORT"),
    ]
    exterior_number: Annotated[
        str,
        Field(max_length=20, description="Número exterior (o S/N)"),
        _required("ADDRESS_EXTERIOR_NUMBER_REQUIRED", min_length=1),
    ]
    interior_number: Annotated[
        str | None, Field(max_length=20, description="Número interior"), AfterValidator(_optional)
    ] = None
    reference_notes: Annotated[
        str | None,
        Field(max_length=300, description="Referencias para llegar: entrecalles o puntos cercanos (opcional)"),
        AfterValidator(_notes),
    ] = None
    latitude: float | None = Field(default=None, ge=-90, le=90, description="Latitud WGS84")
    longitude: float | None = Field(default=None, ge=-180, le=180, description="Longitud WGS84")

    @field_validator("postal_code")
    @classmethod
    def _postal_code(cls, value: str, info: ValidationInfo) -> str:
        code = " ".join(value.split()).upper()
        pattern, key = _POSTAL_CODES.get(info.data.get("country_code", ""), (_POSTAL_CODE_ANY, "POSTAL_CODE_INVALID"))
        if not pattern.match(code):
            raise LocalizedValueError(key)
        return code

    @model_validator(mode="after")
    def _point(self) -> Self:
        if (self.latitude is None) != (self.longitude is None):
            raise LocalizedValueError("COORDINATES_INCOMPLETE")
        return self


#: Columnas del domicilio en la base (mismos nombres que este esquema y que `AddressMixin`).
ADDRESS_FIELDS = tuple(Address.model_fields)


def apply_address(target: Any, address: Address) -> None:
    """Copia el domicilio al registro (validador o sitio de trabajo)."""
    for field in ADDRESS_FIELDS:
        setattr(target, field, getattr(address, field))


def address_of(target: Any) -> Address | None:
    """El domicilio guardado, sin validarlo de nuevo (un país pudo desactivarse en el catálogo después, o
    el domicilio es de antes de pedir la colonia)."""
    if not target.street:
        return None
    return Address.model_construct(**{field: getattr(target, field) for field in ADDRESS_FIELDS})
