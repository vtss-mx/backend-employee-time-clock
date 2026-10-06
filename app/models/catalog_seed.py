"""Registros de los catálogos: `alembic/seed/catalogs.json` es su única fuente.

La migración que crea los catálogos los carga de ese archivo, las pruebas también (create_all +
`seed_catalogs`) y `scripts/quality.sh` verifica que una base migrada tenga exactamente esos
registros. Sus textos en inglés viven al lado, en `catalogs.en-US.json` (tabla `catalog.translations`).
Cambiar un catálogo = editar el JSON (y su texto en cada idioma) y agregar una migración que lleve las
bases existentes al mismo contenido.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, insert

from app.core.database import Base, engine
from app.core.db_schemas import CATALOG
from app.models.catalog import TRANSLATION_LOCALES, CatalogTranslation

SEED_DIR = Path(__file__).resolve().parents[2] / "alembic" / "seed"
SEED_FILE = SEED_DIR / "catalogs.json"
#: Textos de los catálogos en cada idioma que no es el de omisión (el español vive en `catalogs.json`), uno por
#: archivo: `{catálogo: {código: {columna: texto}}}`. Un archivo por idioma para que su ortografía se revise con el
#: diccionario de ese idioma (cspell) y para que agregar un idioma sea agregar un archivo.
TRANSLATION_FILES = {locale: SEED_DIR / f"catalogs.{locale}.json" for locale in TRANSLATION_LOCALES}


@cache
def load_catalog_seed() -> dict[str, list[dict[str, Any]]]:
    """{tabla del esquema catalog: [registro, ...]}, en orden de carga (las referenciadas primero)."""
    data: dict[str, list[dict[str, Any]]] = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    return data


@cache
def load_translation_seed() -> dict[str, dict[str, dict[str, dict[str, str]]]]:
    """{idioma: {catálogo: {código: {columna: texto}}}}: los textos de `catalog.translations`."""
    return {locale: json.loads(path.read_text(encoding="utf-8")) for locale, path in TRANSLATION_FILES.items()}


def translation_rows() -> list[dict[str, str]]:
    """Las filas de `catalog.translations` (las carga la migración 0064 y `seed_catalogs`)."""
    return [
        {"catalog": catalog, "code": code, "locale": locale, "field": field, "text": text}
        for locale, catalogs in load_translation_seed().items()
        for catalog, codes in catalogs.items()
        for code, fields in codes.items()
        for field, text in fields.items()
    ]


def seed_catalogs(target: Engine = engine) -> None:
    """Carga los catálogos (y sus traducciones) en una base creada con create_all."""
    with target.begin() as connection:
        for table, rows in load_catalog_seed().items():
            connection.execute(insert(Base.metadata.tables[f"{CATALOG}.{table}"]), rows)
        connection.execute(
            insert(Base.metadata.tables[f"{CATALOG}.{CatalogTranslation.__tablename__}"]), translation_rows()
        )


def create_schema(target: Engine = engine) -> None:
    """Base completa desde los modelos (pruebas y quality.sh): estructura y catálogos."""
    Base.metadata.create_all(target)
    seed_catalogs(target)
