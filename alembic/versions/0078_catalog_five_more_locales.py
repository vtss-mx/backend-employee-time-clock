"""Cinco idiomas más en los catálogos: pt-BR, fr-FR, de-DE, it-IT y es-ES (regla 16 de la raíz)

Decisión del dueño del producto (2026-10-06): la plataforma habla siete idiomas (es-MX, en-US, pt-BR, fr-FR, de-DE,
it-IT y es-ES; registro y glosario en `docs/i18n/glosario.md`). Los textos de los catálogos de la BD (nombres de
pantallas y módulos del menú, estados, motivos, errores de la captura, monedas, países...) viven en
`catalog.translations` (una fila por catálogo, registro, idioma y columna; migración `0064`): un idioma nuevo no cambia
la estructura de ningún catálogo, solo agrega sus filas y su valor al CHECK `locale`.

- El CHECK `ck_translations_locale` pasa de `('en-US')` a los seis idiomas que no son el de omisión (el español de
  México sigue en las columnas de cada catálogo).
- Las filas salen de `alembic/seed/catalogs.<idioma>.json` (al lado de `catalogs.json` y `catalogs.en-US.json`), solo de
  los registros que ya existen en la base al aplicar esta migración (un catálogo que crea una migración posterior aún no
  existe en una base nueva: se salta con `to_regclass`, como en `0064`, y esa migración carga sus traducciones).
  `tests/test_i18n.py` exige que cada texto de cada registro tenga su traducción en cada idioma y que quepa en su columna.
- `downgrade` borra las filas de los cinco idiomas y regresa el CHECK a `('en-US')`: la API anterior solo habla es-MX y
  en-US y no las lee.

Sin SQL escrita con datos (regla 21): las sentencias son expresiones de SQLAlchemy, los códigos y textos viajan como
parámetros (`bulk_insert`, `delete ... in_`) y el CHECK se arma solo con las constantes de este archivo. El nombre de
cada catálogo sale del archivo de semillas del repositorio (`sa.table`), nunca de una petición.

Revision ID: 0078
Revises: 0077
Create Date: 2026-10-06 23:30:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
#: Los idiomas con traducción antes de esta migración y los que agrega (el de omisión, es-MX, no se traduce).
PREVIOUS_LOCALES = ("en-US",)
NEW_LOCALES = ("pt-BR", "fr-FR", "de-DE", "it-IT", "es-ES")
LOCALES = PREVIOUS_LOCALES + NEW_LOCALES

_TRANSLATIONS = sa.table(
    "translations",
    sa.column("catalog"),
    sa.column("code"),
    sa.column("locale"),
    sa.column("field"),
    sa.column("text"),
    schema=CATALOG,
)


def _in(values: Sequence[str]) -> str:
    """Lista de literales para el CHECK, solo con las constantes de este archivo."""
    return ", ".join(repr(value) for value in values)


def _rows(locale: str) -> list[dict[str, str]]:
    """Las traducciones del archivo del idioma cuyos registros ya existen en la base."""
    seed = json.loads((SEED_DIR / f"catalogs.{locale}.json").read_text(encoding="utf-8"))
    bind = op.get_bind()
    rows: list[dict[str, str]] = []
    for catalog, codes in seed.items():
        if bind.execute(sa.text("SELECT to_regclass(:name)"), {"name": f"{CATALOG}.{catalog}"}).scalar() is None:
            continue
        table = sa.table(catalog, sa.column("code"), schema=CATALOG)
        existing = set(bind.execute(sa.select(table.c.code)).scalars())
        rows.extend(
            {"catalog": catalog, "code": code, "locale": locale, "field": field, "text": text}
            for code, fields in codes.items()
            if code in existing
            for field, text in fields.items()
        )
    return rows


def _replace_locale_check(locales: Sequence[str]) -> None:
    """El CHECK con la lista de idiomas (`op.f`: el nombre ya es el final, como lo creó la `0064`)."""
    name = op.f("ck_translations_locale")
    op.drop_constraint(name, "translations", schema=CATALOG, type_="check")
    op.create_check_constraint(name, "translations", f"locale IN ({_in(locales)})", schema=CATALOG)


def upgrade() -> None:
    _replace_locale_check(LOCALES)
    for locale in NEW_LOCALES:
        rows = _rows(locale)
        if rows:
            op.bulk_insert(_TRANSLATIONS, rows)


def downgrade() -> None:
    op.execute(sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.locale.in_(NEW_LOCALES)))
    _replace_locale_check(PREVIOUS_LOCALES)
