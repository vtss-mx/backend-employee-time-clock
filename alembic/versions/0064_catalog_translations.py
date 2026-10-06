"""Catálogos en inglés: tabla `catalog.translations` (regla 16 de la raíz: la API responde en es-MX o en-US)

Decisión del dueño del producto: todo lo que una persona lee existe en español de México y en inglés de Estados
Unidos, también los textos de los catálogos que sirve la API (nombres de pantallas y módulos del menú, estados,
motivos, errores de la captura facial, monedas, países...). Diseño (detalle en el README, "Idiomas"):

- **Una sola tabla para todos los catálogos**: `catalog.translations (catalog, code, locale, field) → text`. El
  español se queda en las columnas de cada catálogo (es el idioma de omisión y lo que leen la lógica y las llaves
  foráneas); aquí solo lo que cambia con el idioma. Agregar un idioma o un catálogo no cambia la estructura de
  ninguno (otra opción, una columna `name_en`, `description_en`... por cada texto de cada catálogo, eran ~60 tablas
  que cambiar y otra vez por cada idioma).
- CHECK de los idiomas (`en-US`) y de las columnas que se traducen: una fila mal escrita no entra. Sin llave foránea
  hacia cada catálogo (una tabla apunta a muchos): `tests/test_i18n.py` exige que cada texto de cada registro tenga
  su traducción, que ninguna sobre y que quepa en su columna.
- Sin índices además de la llave primaria: la caché de catálogos la lee completa al recargar (una consulta más cada
  `CATALOG_CACHE_SECONDS`, ninguna por petición). Tabla diminuta (~1 000 filas) y de solo lectura para la API (los
  permisos del esquema `catalog` los pone `db roles`).
- Los textos salen de `alembic/seed/catalogs.en-US.json` (al lado de `catalogs.json`), solo de los registros que ya
  existen en la base al aplicar esta migración: una migración posterior que agregue un registro agrega su traducción.
- **Excepción a «nunca editar una migración aplicada»** (aprobada por el orquestador, 2026-10-06, con la migración
  0074): el archivo de traducciones es el VIGENTE, así que un catálogo NUEVO (una tabla que crea una migración
  posterior, p. ej. `tax_id_types` en 0074) rompía una base nueva aquí: `SELECT code FROM catalog.<tabla>` sobre una
  tabla que todavía no existe. Ahora se saltan las tablas que aún no existen (`to_regclass`); la migración que crea el
  catálogo carga sus registros y sus traducciones. Para una base que ya aplicó 0064 no cambia nada (no se vuelve a
  correr) y, en una base nueva, carga exactamente las mismas filas que antes para las tablas que existían. Probado
  con una base nueva y con una base en 0073 con datos (upgrade, downgrade y upgrade otra vez; firma = modelos).

`downgrade` borra la tabla: la API anterior no la lee (responde todo en español).

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-05 09:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
LOCALES = ("en-US",)
FIELDS = ("name", "description", "message", "phrase", "instruction", "employee_note", "short_name")


def _in(values: Sequence[str]) -> str:
    return ", ".join(repr(value) for value in values)


def _rows(locale: str) -> list[dict[str, str]]:
    """Las traducciones del archivo del idioma cuyos registros ya existen en la base (un catálogo que crea una migración
    posterior todavía no existe: se salta y esa migración carga sus traducciones)."""
    seed = json.loads((SEED_DIR / f"catalogs.{locale}.json").read_text(encoding="utf-8"))
    bind = op.get_bind()
    rows = []
    for catalog, codes in seed.items():
        if bind.execute(sa.text("SELECT to_regclass(:name)"), {"name": f"{CATALOG}.{catalog}"}).scalar() is None:
            continue
        existing = set(bind.execute(sa.text(f"SELECT code FROM {CATALOG}.{catalog}")).scalars())
        rows.extend(
            {"catalog": catalog, "code": code, "locale": locale, "field": field, "text": text}
            for code, fields in codes.items()
            if code in existing
            for field, text in fields.items()
        )
    return rows


def upgrade() -> None:
    translations = op.create_table(
        "translations",
        sa.Column("catalog", sa.String(length=40), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("locale", sa.String(length=5), nullable=False),
        sa.Column("field", sa.String(length=20), nullable=False),
        sa.Column("text", sa.String(length=300), nullable=False),
        sa.CheckConstraint(f"locale IN ({_in(LOCALES)})", name=op.f("ck_translations_locale")),
        sa.CheckConstraint(f"field IN ({_in(FIELDS)})", name=op.f("ck_translations_field")),
        sa.PrimaryKeyConstraint("catalog", "code", "locale", "field", name=op.f("pk_translations")),
        schema=CATALOG,
    )
    for locale in LOCALES:
        op.bulk_insert(translations, _rows(locale))


def downgrade() -> None:
    op.drop_table("translations", schema=CATALOG)
