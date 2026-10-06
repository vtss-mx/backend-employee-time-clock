"""Identificador fiscal de la empresa de cualquier país (decisión del dueño del producto, 2026-10-06)

«Puede ser RFC o algún otro identificador que se use para poder registrar una empresa de cualquier parte del mundo».
El dueño eligió país fiscal + tipo de identificador + número (detalle en el README, «Identificador fiscal de la
empresa»):

- `catalog.tax_id_types` (nuevo): los tipos por país con su regla de formato como DATO (`pattern`, `min_length`,
  `max_length`, `example`) y sus textos (`name`, `short_name` y `description`, el formato en palabras) con su
  traducción a en-US: RFC (MX), EIN (US), BN (CA), NIF (ES), NIT (CO), CUIT (AR), RUT (CL), RUC (PE), CNPJ (BR), VAT y
  CRN (GB), USt-IdNr. (DE), SIREN y SIRET (FR) y «Otro identificador fiscal» (`OTHER`, de cualquier país). El dígito
  verificador de los que lo tienen y las reglas propias del RFC viven en el código (`app/schemas/tax_ids.py`).
- `tenancy.companies`: `tax_country` (FK a `countries`), `tax_id_type` (FK a `tax_id_types`) y `tax_id` (el número
  normalizado), nulas; CHECK `tax_id`: los tres juntos o ninguno (sin número no se guardan ni país ni tipo).
- Los RFC que ya existen pasan a ser su identificador: `tax_id = rfc`, tipo `MX_RFC`, país `MX` (también los de las
  empresas en «Eliminados»: restaurarlas revisa su identificador).
- `uq_companies_tax_id`: único entre las empresas VIGENTES que lo capturaron, por (país, tipo, número), parcial
  `deleted_at IS NULL AND tax_id IS NOT NULL` (regla 20: el dato de una eliminada se puede volver a usar).
- `ix_companies_search_trgm` (la búsqueda por nombre, razón social e identificador) cambia `rfc` por `tax_id` en su
  expresión. Se reemplaza en la misma transacción (la consulta nunca se queda sin índice) y sin `CONCURRENTLY`: la tabla
  es pequeña (una fila por empresa cliente) y la operación dura milisegundos.

**Compatibilidad con la versión anterior en marcha** (agregar antes de quitar, `backend-employee-time-clock/AGENTS.md`
§4): `rfc` y su único `ix_companies_rfc` se quedan una versión. El código nuevo escribe los dos (`Company.set_tax`: el
número en `rfc` si el tipo es RFC, si no NULL) y ya no lee `rfc`; la versión anterior sigue leyendo y revisando su RFC.
Una migración posterior (README, «Pendiente») vuelve a copiar `rfc` → identificador en las filas que la versión anterior
haya escrito durante el despliegue y quita la columna y su índice.

**Catálogo nuevo y la migración 0064**: 0064 carga las traducciones de TODOS los catálogos del archivo vigente; en una
base nueva esta tabla aún no existe a esa altura. Se corrigió 0064 para que salte las tablas que todavía no existen
(excepción aprobada por el orquestador y documentada en su docstring; para una base que ya la aplicó no cambia nada) y
esta migración carga sus registros y sus traducciones.

`downgrade` regresa la búsqueda a `rfc` y quita columnas, índice, restricción, catálogo y traducciones. Los
identificadores que no son RFC se pierden (la versión anterior solo conoce `rfc`).

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-06 12:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
TENANCY = "tenancy"
LOCALE = "en-US"
TYPES = "tax_id_types"
#: Los RFC que ya existen, como su identificador (las tres columnas en una sentencia).
BACKFILL = (
    "UPDATE tenancy.companies SET tax_country = 'MX', tax_id_type = 'MX_RFC', tax_id = rfc "
    "WHERE rfc IS NOT NULL AND tax_id IS NULL"
)
#: La búsqueda de empresas (`company_search_text`) con el identificador y, para `downgrade`, con el RFC.
SEARCH_INDEX = (
    "CREATE INDEX ix_companies_search_trgm ON tenancy.companies USING gin "
    "((lower(name || ' ' || coalesce(legal_name, '') || ' ' || coalesce(tax_id, ''))) gin_trgm_ops)"
)
PREVIOUS_SEARCH_INDEX = (
    "CREATE INDEX ix_companies_search_trgm ON tenancy.companies USING gin "
    "((lower(name || ' ' || coalesce(legal_name, '') || ' ' || coalesce(rfc, ''))) gin_trgm_ops)"
)
DROP_SEARCH_INDEX = "DROP INDEX IF EXISTS tenancy.ix_companies_search_trgm"
TAX_ID_LIVE = "deleted_at IS NULL AND tax_id IS NOT NULL"
_TRANSLATIONS = sa.table(
    "translations",
    sa.column("catalog"),
    sa.column("code"),
    sa.column("locale"),
    sa.column("field"),
    sa.column("text"),
    schema=CATALOG,
)


def _seed(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SEED_DIR / name).read_text(encoding="utf-8"))
    return data


def _catalog() -> None:
    """El catálogo con sus registros y sus textos en inglés (en una base nueva, 0064 lo saltó: aún no existía)."""
    table = op.create_table(
        TYPES,
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("short_name", sa.String(length=30), nullable=False),
        sa.Column("pattern", sa.String(length=200), nullable=False),
        sa.Column("min_length", sa.SmallInteger(), nullable=False),
        sa.Column("max_length", sa.SmallInteger(), nullable=False),
        sa.Column("example", sa.String(length=30), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.CheckConstraint(
            "min_length >= 1 AND max_length >= min_length AND max_length <= 30", name=op.f("ck_tax_id_types_lengths")
        ),
        sa.ForeignKeyConstraint(
            ["country_code"], [f"{CATALOG}.countries.code"], name=op.f("fk_tax_id_types_country_code_countries")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_tax_id_types")),
        schema=CATALOG,
    )
    op.bulk_insert(table, _seed("catalogs.json")[TYPES])
    english = _seed(f"catalogs.{LOCALE}.json")[TYPES]
    op.bulk_insert(
        _TRANSLATIONS,
        [
            {"catalog": TYPES, "code": code, "locale": LOCALE, "field": field, "text": text}
            for code, fields in english.items()
            for field, text in fields.items()
        ],
    )


def _companies() -> None:
    """Las tres columnas, los RFC que ya existen, la restricción y el único de lo vigente."""
    op.add_column("companies", sa.Column("tax_country", sa.String(length=2), nullable=True), schema=TENANCY)
    op.add_column("companies", sa.Column("tax_id_type", sa.String(length=30), nullable=True), schema=TENANCY)
    op.add_column("companies", sa.Column("tax_id", sa.String(length=30), nullable=True), schema=TENANCY)
    op.create_foreign_key(
        op.f("fk_companies_tax_country_countries"),
        "companies",
        "countries",
        ["tax_country"],
        ["code"],
        source_schema=TENANCY,
        referent_schema=CATALOG,
    )
    op.create_foreign_key(
        op.f("fk_companies_tax_id_type_tax_id_types"),
        "companies",
        TYPES,
        ["tax_id_type"],
        ["code"],
        source_schema=TENANCY,
        referent_schema=CATALOG,
    )
    op.execute(BACKFILL)
    op.create_check_constraint(
        op.f("ck_companies_tax_id"),
        "companies",
        "(tax_id IS NULL) = (tax_id_type IS NULL) AND (tax_id IS NULL) = (tax_country IS NULL)",
        schema=TENANCY,
    )
    op.create_index(
        "uq_companies_tax_id",
        "companies",
        ["tax_country", "tax_id_type", "tax_id"],
        unique=True,
        schema=TENANCY,
        postgresql_where=sa.text(TAX_ID_LIVE),
    )


def upgrade() -> None:
    _catalog()
    _companies()
    op.execute(DROP_SEARCH_INDEX)
    op.execute(SEARCH_INDEX)


def downgrade() -> None:
    op.execute(DROP_SEARCH_INDEX)
    op.execute(PREVIOUS_SEARCH_INDEX)
    op.drop_index("uq_companies_tax_id", table_name="companies", schema=TENANCY)
    op.drop_constraint(op.f("ck_companies_tax_id"), "companies", type_="check", schema=TENANCY)
    op.drop_constraint(op.f("fk_companies_tax_id_type_tax_id_types"), "companies", type_="foreignkey", schema=TENANCY)
    op.drop_constraint(op.f("fk_companies_tax_country_countries"), "companies", type_="foreignkey", schema=TENANCY)
    for column in ("tax_id", "tax_id_type", "tax_country"):
        op.drop_column("companies", column, schema=TENANCY)
    op.execute(sa.text("DELETE FROM catalog.translations WHERE catalog = :catalog").bindparams(catalog=TYPES))
    op.drop_table(TYPES, schema=CATALOG)
