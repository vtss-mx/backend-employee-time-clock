"""Identificador fiscal de una empresa de cualquier país (migración 0074; decisión del dueño del producto, 2026-10-06:
«puede ser RFC o algún otro identificador que se use para poder registrar una empresa de cualquier parte del mundo»).

Es país fiscal + tipo (`catalog.tax_id_types`) + número, con UNA regla para el alta, la edición, la restauración, la
validación en vivo y la API:

- **Opcional**, como el RFC de antes: sin número no hay identificador y el país y el tipo se ignoran (los tres se
  guardan juntos o ninguno; CHECK `tax_id` de la tabla). Con número y sin tipo, el principal del país (el primero de
  los suyos en el catálogo; «Otro identificador fiscal» si no tiene ninguno); sin país, el del tipo o, si no lo dice,
  México (el país de la plataforma): lo mismo que propone la aplicación web.
- **Normalización** igual para todos los tipos: mayúsculas, sin espacios, guiones, puntos ni diagonales
  (`11.222.333/0001-81` → `11222333000181`). Así se guarda, se compara (único por país, tipo y número) y se busca.
- **Formato**: el del catálogo (`pattern` completo, `min_length`, `max_length`): es dato, no código.
- **RFC** (`MX_RFC`): su validación completa de siempre (`normalize_company_rfc`: 12 o 13 caracteres, sin RFC
  genéricos y con su fecha válida), con sus mismos mensajes.
- **Dígito verificador** solo donde el algoritmo es público, conocido y barato (`CHECK_DIGITS`): CUIT (Argentina),
  RUT (Chile), NIT (Colombia), RUC (Perú), CNPJ (Brasil, también el alfanumérico vigente desde julio de 2026), SIREN y
  SIRET (Francia, Luhn; los SIRET de La Poste suman múltiplo de 5). Los demás, solo formato: EIN (Estados Unidos), BN
  (Canadá), NIF (España), VAT y CRN (Reino Unido), USt-IdNr. (Alemania) y «Otro».
"""

import re
from collections.abc import Callable, Sequence
from typing import Any, Self

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.i18n import LocalizedValueError
from app.models.company import RFC_TAX_ID_TYPE, TaxId
from app.schemas.validators import luhn_valid, normalize_company_rfc
from app.services.catalog_service import get_catalogs

#: País fiscal cuando llega el número sin él (el de la plataforma; la aplicación web lo propone igual).
DEFAULT_TAX_COUNTRY = "MX"
#: Tipos que la lógica nombra (el resto es dato del catálogo).
RFC_TYPE = RFC_TAX_ID_TYPE
OTHER_TYPE = "OTHER"
#: Lo que se ignora al escribir el número: espacios, guiones, puntos y diagonales.
_SEPARATORS = re.compile(r"[\s./-]")
_TAX_ID_TYPES = "tax_id_types"


def clean_tax_id(value: str) -> str:
    """El número como se guarda (sin validarlo todavía): mayúsculas, sin separadores."""
    return _SEPARATORS.sub("", value).upper()


def is_blank_tax_id(value: str | None) -> bool:
    """Sin capturar: nada, o solo separadores."""
    return not clean_tax_id(value or "")


# ---------- Dígitos verificadores (solo los conocidos y baratos) ----------

#: Pesos de CUIT (Argentina) y RUC (Perú) sobre sus primeros 10 dígitos.
_MOD11_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
#: Pesos del NIT (Colombia), del dígito de las unidades hacia la izquierda.
_NIT_WEIGHTS = (3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71)
#: Pesos del segundo dígito del CNPJ (Brasil); el primero usa los mismos sin el inicial.
_CNPJ_WEIGHTS = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
#: SIREN de La Poste: sus establecimientos (SIRET) no siguen Luhn, sino suma de dígitos múltiplo de 5.
_LA_POSTE_SIREN = "356000000"


def _weighted(values: Sequence[int], weights: Sequence[int]) -> int:
    return sum(value * weight for value, weight in zip(values, weights, strict=False))


def _digits(text: str) -> list[int]:
    return [int(char) for char in text]


def _cuit_valid(number: str) -> bool:
    """CUIT (AFIP): 11 - (suma ponderada módulo 11); 11 equivale a 0 y 10 no se asigna."""
    check = 11 - _weighted(_digits(number[:10]), _MOD11_WEIGHTS) % 11
    return check != 10 and check % 11 == int(number[10])


def _ruc_valid(number: str) -> bool:
    """RUC (SUNAT): 11 - (suma ponderada módulo 11); 10 equivale a 0 y 11 a 1."""
    return (11 - _weighted(_digits(number[:10]), _MOD11_WEIGHTS) % 11) % 10 == int(number[10])


def _rut_valid(number: str) -> bool:
    """RUT (SII): pesos 2 a 7 cíclicos desde la derecha; 11 - (suma módulo 11), con 11 = 0 y 10 = K."""
    total = sum(int(char) * (2 + position % 6) for position, char in enumerate(reversed(number[:-1])))
    return "0123456789K0"[11 - total % 11] == number[-1]


def _nit_valid(number: str) -> bool:
    """NIT (DIAN): suma ponderada módulo 11 del cuerpo; 0 y 1 quedan igual y lo demás es 11 - residuo."""
    remainder = _weighted(_digits(number[-2::-1]), _NIT_WEIGHTS) % 11
    return (remainder if remainder < 2 else 11 - remainder) == int(number[-1])


def _cnpj_digit(values: Sequence[int], weights: Sequence[int]) -> int:
    remainder = _weighted(values, weights) % 11
    return 0 if remainder < 2 else 11 - remainder


def _cnpj_valid(number: str) -> bool:
    """CNPJ (Receita Federal): dos dígitos módulo 11. Cada carácter vale su código ASCII menos 48 (los dígitos, lo
    mismo que antes; las letras del CNPJ alfanumérico de 2026, A = 17, B = 18...)."""
    values = [ord(char) - 48 for char in number[:12]]
    first = _cnpj_digit(values, _CNPJ_WEIGHTS[1:])
    return number[12:] == f"{first}{_cnpj_digit([*values, first], _CNPJ_WEIGHTS)}"


def _siret_valid(number: str) -> bool:
    """SIRET (INSEE): Luhn, salvo los de La Poste (su SIREN y suma de dígitos múltiplo de 5)."""
    return luhn_valid(number) or (number.startswith(_LA_POSTE_SIREN) and sum(_digits(number)) % 5 == 0)


#: Tipo → su dígito verificador. Una prueba exige que cada uno exista en el catálogo.
CHECK_DIGITS: dict[str, Callable[[str], bool]] = {
    "AR_CUIT": _cuit_valid,
    "CL_RUT": _rut_valid,
    "CO_NIT": _nit_valid,
    "PE_RUC": _ruc_valid,
    "BR_CNPJ": _cnpj_valid,
    "FR_SIREN": luhn_valid,
    "FR_SIRET": _siret_valid,
}


# ---------- País, tipo y número ----------


def tax_country(code: str) -> str:
    """País fiscal capturado: activo en el catálogo de países."""
    country = code.strip().upper()
    if not get_catalogs().is_active("countries", country):
        raise LocalizedValueError("COUNTRY_INVALID")
    return country


def main_tax_id_type(country: str) -> str:
    """El tipo que se propone para un país: el primero de los suyos (orden del catálogo) o «Otro»."""
    rows = get_catalogs().entries[_TAX_ID_TYPES]
    return next((row["code"] for row in rows if row["active"] and row["country_code"] == country), OTHER_TYPE)


def tax_id_type(code: str, country: str | None) -> str:
    """Tipo capturado: activo y del país (o de cualquier país). Sin país (o con uno que ya tiene su error), solo que
    exista."""
    catalogs = get_catalogs()
    code = code.strip().upper()
    row = catalogs.get(_TAX_ID_TYPES, code)
    if row is None or not row["active"]:
        raise LocalizedValueError("TAX_ID_TYPE_INVALID")
    if country is not None and row["country_code"] not in (None, country):
        params = {"name": row["short_name"], "country": catalogs.name("countries", country)}
        raise LocalizedValueError("TAX_ID_TYPE_COUNTRY", params)
    return code


def normalize_tax_id(type_code: str, value: str) -> str:
    """El número normalizado y validado según su tipo: el RFC con su regla completa; los demás con el formato del
    catálogo y, si lo tienen, su dígito verificador. Vacío no es un número: lo resuelve antes quien llama."""
    number = clean_tax_id(value)
    if type_code == RFC_TYPE:
        return normalize_company_rfc(number)
    row = get_catalogs().get(_TAX_ID_TYPES, type_code)
    if row is None:  # quien llama ya validó el tipo; si el catálogo cambió a la mitad, no hay regla con qué medirlo
        raise LocalizedValueError("TAX_ID_TYPE_INVALID")
    name, low, high = row["short_name"], row["min_length"], row["max_length"]
    if not low <= len(number) <= high:
        if low == high:
            raise LocalizedValueError("TAX_ID_LENGTH_EXACT", {"name": name, "length": low})
        raise LocalizedValueError("TAX_ID_LENGTH", {"name": name, "min": low, "max": high})
    if not re.fullmatch(row["pattern"], number):
        raise LocalizedValueError("TAX_ID_FORMAT", {"name": name, "example": row["example"]})
    check = CHECK_DIGITS.get(type_code)
    if check is not None and not check(number):
        raise LocalizedValueError("TAX_ID_CHECK_DIGIT", {"name": name})
    return number


def tax_id_defaults(country: str | None, type_code: str | None) -> tuple[str, str]:
    """País y tipo de un número capturado (ya validados los que llegaron), con los de omisión de los que falten: sin
    tipo, el principal del país (o de México); sin país, el del tipo (o México, si el tipo sirve para cualquiera)."""
    resolved_type = type_code or main_tax_id_type(country or DEFAULT_TAX_COUNTRY)
    type_country = (get_catalogs().get(_TAX_ID_TYPES, resolved_type) or {}).get("country_code")
    return country or type_country or DEFAULT_TAX_COUNTRY, resolved_type


def resolve_tax_id(country: str | None, type_code: str | None, number: str) -> TaxId:
    """El identificador completo de un número capturado (no vacío): país y tipo validados (o los de omisión) y el
    número según su tipo. La misma regla que `TaxIdFields`, para la validación en vivo."""
    checked_country = tax_country(country) if country else None
    checked_type = tax_id_type(type_code, checked_country) if type_code else None
    resolved_country, resolved_type = tax_id_defaults(checked_country, checked_type)
    return TaxId(resolved_country, resolved_type, normalize_tax_id(resolved_type, number))


def tax_id_name(type_code: str) -> str:
    """La sigla del tipo en el idioma de la petición («RFC», «EIN»); el código si ya no está en el catálogo."""
    row = get_catalogs().get(_TAX_ID_TYPES, type_code)
    return row["short_name"] if row else type_code


# ---------- Contrato de la API (alta y edición de empresas) ----------

#: Los campos del identificador fiscal en el cuerpo (el campo anterior `rfc` solo cuenta si no llega ninguno).
TAX_ID_FIELDS = frozenset({"tax_country", "tax_id_type", "tax_id"})


class TaxIdFields(BaseModel):
    """País fiscal + tipo + número de la empresa (alta y edición). Los campos van en este orden: cada uno se valida con
    los anteriores ya validados (`info.data`, como el código postal con su país), así que cada error queda en su campo.

    Sin número se ignoran el país y el tipo (los tres quedan en null). En la edición, el identificador se cambia solo si
    llega `tax_id` (null o vacío lo borra); el país o el tipo sin el número no cambian nada.
    `rfc` (obsoleto, se quita junto con la columna anterior): si llega sin ninguno de los tres, equivale a
    `tax_country=MX`, `tax_id_type=MX_RFC` y `tax_id=<rfc>` (la aplicación web anterior, en marcha durante un
    despliegue).
    """

    tax_country: str | None = Field(
        default=None,
        max_length=2,
        description="País fiscal (ISO 3166-1 alfa-2, `catalog.countries`). Con número y sin país: el del tipo o MX",
        examples=["MX"],
    )
    tax_id_type: str | None = Field(
        default=None,
        max_length=30,
        description="Tipo de identificador (`catalog.tax_id_types`, del país o `OTHER`). Con número y sin tipo: el "
        "principal del país",
        examples=["MX_RFC"],
    )
    tax_id: str | None = Field(
        default=None,
        max_length=40,
        description="Opcional. Número; se guarda en mayúsculas, sin espacios, guiones, puntos ni diagonales. Vacío o "
        "null = sin capturar",
        examples=["PNO120315AB1"],
    )
    rfc: str | None = Field(
        default=None,
        max_length=20,
        description="Obsoleto: usa `tax_country`, `tax_id_type` y `tax_id`. Solo cuenta si no llega ninguno de ellos",
        json_schema_extra={"deprecated": True},
    )

    @model_validator(mode="before")
    @classmethod
    def _legacy_rfc(cls, data: Any) -> Any:
        if isinstance(data, dict) and "rfc" in data and not TAX_ID_FIELDS & data.keys():
            return {**data, "tax_country": DEFAULT_TAX_COUNTRY, "tax_id_type": RFC_TYPE, "tax_id": data["rfc"]}
        return data

    @field_validator("tax_country")
    @classmethod
    def _tax_country(cls, value: str | None) -> str | None:
        return tax_country(value) if value and value.strip() else None

    @field_validator("tax_id_type")
    @classmethod
    def _tax_id_type(cls, value: str | None, info: ValidationInfo) -> str | None:
        if not value or not value.strip():
            return None
        # Sin país (o con uno inválido, que ya tiene su error) solo se revisa que el tipo exista.
        return tax_id_type(value, info.data.get("tax_country"))

    @field_validator("tax_id")
    @classmethod
    def _tax_id(cls, value: str | None, info: ValidationInfo) -> str | None:
        if is_blank_tax_id(value):
            return None
        if not {"tax_country", "tax_id_type"} <= info.data.keys():
            return value  # el país o el tipo ya tienen su error: el número no se mide contra otro tipo
        _, type_code = tax_id_defaults(info.data["tax_country"], info.data["tax_id_type"])
        return normalize_tax_id(type_code, str(value))

    @model_validator(mode="after")
    def _together(self) -> Self:
        """Los tres juntos: sin número, ni país ni tipo; con número, los de omisión que falten."""
        if self.tax_id is None:
            self.tax_country = self.tax_id_type = None
        else:
            self.tax_country, self.tax_id_type = tax_id_defaults(self.tax_country, self.tax_id_type)
        return self

    def tax(self) -> TaxId | None:
        """El identificador capturado (ya completo y validado), o None."""
        if self.tax_country and self.tax_id_type and self.tax_id:
            return TaxId(self.tax_country, self.tax_id_type, self.tax_id)
        return None
