"""Registros de los catálogos: `alembic/seed/catalogs.json` es su única fuente.

La migración que crea los catálogos los carga de ese archivo, las pruebas también (create_all +
`seed_catalogs`) y `scripts/quality.sh` verifica que una base migrada tenga exactamente esos
registros. Cambiar un catálogo = editar el JSON y agregar una migración que lleve las bases
existentes al mismo contenido.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, insert

from app.core.database import Base, engine
from app.core.db_schemas import CATALOG

SEED_FILE = Path(__file__).resolve().parents[2] / "alembic" / "seed" / "catalogs.json"


@cache
def load_catalog_seed() -> dict[str, list[dict[str, Any]]]:
    """{tabla del esquema catalog: [registro, ...]}, en orden de carga (las referenciadas primero)."""
    data: dict[str, list[dict[str, Any]]] = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    return data


def seed_catalogs(target: Engine = engine) -> None:
    """Carga los catálogos en una base creada con create_all."""
    with target.begin() as connection:
        for table, rows in load_catalog_seed().items():
            connection.execute(insert(Base.metadata.tables[f"{CATALOG}.{table}"]), rows)


def create_schema(target: Engine = engine) -> None:
    """Base completa desde los modelos (pruebas y quality.sh): estructura y catálogos."""
    Base.metadata.create_all(target)
    seed_catalogs(target)
