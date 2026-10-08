"""Guía por audio del registro facial y voces configurables por empresa (decisión del dueño del producto, 2026-10-08)

El registro facial puede DICTAR sus indicaciones con voz («quédate quieto», «voltea a la derecha»...). La síntesis ocurre
en el navegador (nada sale del servidor, regla 13); el servidor solo guarda, por empresa, si se dicta y con cuál voz.

- `catalog.voice_profiles` (nuevo): las voces que el ADMIN puede elegir (femenina cálida, clara, masculina serena,
  grave y la neutra del sistema). `name` es el nombre que ve el ADMIN y `description` su aclaración; la app mapea cada
  código a los parámetros de la voz (género, tono, velocidad). Sus traducciones en `alembic/seed/catalogs.<idioma>.json`.
- `tenancy.verification_policy.voice_guidance_enabled` (nuevo, APAGADO por omisión): la empresa enciende la guía por
  audio; es una ayuda de accesibilidad, no un candado de seguridad (no pasa por la regla de dos personas).
- `tenancy.verification_policy.voice_profile` (nuevo, por omisión `FEMALE_WARM`): la voz elegida. Sin llave foránea
  (como los modos de señal): el servicio valida que sea un código activo del catálogo (422 `INVALID_VOICE_PROFILE`).

Compatible con la versión anterior en marcha: columnas con valor por omisión y una tabla de catálogo nueva. Los permisos
de la API sobre el catálogo nuevo los pone `python -m app.cli db roles` en el mismo despliegue (servicio `migrate`).
`downgrade` quita las columnas, el catálogo y sus traducciones.

Revision ID: 0088
Revises: 0087
Create Date: 2026-10-08 06:10:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
TENANCY = "tenancy"
PROFILES = "voice_profiles"
_TRANSLATIONS = sa.table(
    "translations",
    sa.column("catalog"),
    sa.column("code"),
    sa.column("locale"),
    sa.column("field"),
    sa.column("text"),
    schema=CATALOG,
)


def _seed(name: str) -> dict:
    return json.loads((SEED_DIR / name).read_text(encoding="utf-8"))


def _catalog(seed: dict) -> None:
    profiles = op.create_table(
        PROFILES,
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f(f"pk_{PROFILES}")),
        schema=CATALOG,
    )
    op.bulk_insert(profiles, seed[PROFILES])


def _translations() -> None:
    """Los textos del catálogo nuevo en cada idioma que tiene archivo de traducciones."""
    rows: list[dict[str, str]] = []
    for path in sorted(SEED_DIR.glob("catalogs.*.json")):
        locale = path.name.removeprefix("catalogs.").removesuffix(".json")
        texts = json.loads(path.read_text(encoding="utf-8"))
        rows += [
            {"catalog": PROFILES, "code": code, "locale": locale, "field": field, "text": text}
            for code, fields in texts.get(PROFILES, {}).items()
            for field, text in fields.items()
        ]
    if not rows:
        return
    statement = postgresql.insert(_TRANSLATIONS).values(rows)
    op.execute(
        statement.on_conflict_do_update(
            index_elements=["catalog", "code", "locale", "field"], set_={"text": statement.excluded.text}
        )
    )


def upgrade() -> None:
    _catalog(_seed("catalogs.json"))
    _translations()
    op.add_column(
        "verification_policy",
        sa.Column("voice_guidance_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        schema=TENANCY,
    )
    op.add_column(
        "verification_policy",
        sa.Column("voice_profile", sa.String(length=30), server_default="FEMALE_WARM", nullable=False),
        schema=TENANCY,
    )


def downgrade() -> None:
    op.drop_column("verification_policy", "voice_profile", schema=TENANCY)
    op.drop_column("verification_policy", "voice_guidance_enabled", schema=TENANCY)
    op.execute(sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.catalog == PROFILES))
    op.drop_table(PROFILES, schema=CATALOG)
