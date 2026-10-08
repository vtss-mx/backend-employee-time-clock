"""Destello de colores retirado de la experiencia (decisión del dueño del producto, 2026-10-06)

El dueño probó el proceso biométrico y pidió quitar por completo los fondos de color de la pantalla («aún siguen
apareciendo unos pantallazos de color de fondo azul, morado… eso elimínalo por completo y agrega algo más enterprise»).
La app ya no pinta ningún color; en el servidor el destello (`flash_liveness`) y el destello dictado (`flash_paced`)
quedan APAGADOS en toda empresa y nacen apagados en las nuevas. El código de la fotometría y del destello dictado sigue
disponible (sus pruebas también), pero la aplicación web no lo ofrece: los controles del ADMIN se muestran apagados y
sin cambios, con la nota «Desactivado por decisión del producto».

- `tenancy.verification_policy`: `flash_liveness` → 'OFF' y `flash_paced` → false en TODAS las políticas existentes, y
  esos mismos valores por omisión para las nuevas.
- Los textos de los niveles predefinidos (`catalog.policy_presets`) y de la acción «un paso más» (`catalog.risk_actions`
  STEP_UP) ya no prometen el destello: se reescriben desde los seeds (es-MX en la tabla; los demás idiomas en
  `catalog.translations`). Ningún nivel enciende el destello ni exige sus señales (`FLASH_*`): sin destello nunca se
  miden. Lo que cubre los ataques de presentación: la ráfaga (continuidad, congelado, bucle, pulso), `PERSPECTIVE_FLAT`,
  `MOIRE_HIGH`, `NOISE_MISMATCH`, los movimientos, el reenvío perceptual, la llave del dispositivo, la telemetría y la
  verificación por voz y video del registro (README «Destello retirado»).

Compatible con la versión anterior en marcha (solo cambia datos y valores por omisión). `downgrade` regresa los valores
por omisión anteriores (OBSERVE / true) y deja las políticas y los textos como quedaron: volver a encender el destello
en una empresa es una decisión explícita del ADMIN por la API, no de una migración.

Revision ID: 0080
Revises: 0079
Create Date: 2026-10-07 02:30:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
TENANCY = "tenancy"
#: Los textos que cambian: los tres niveles predefinidos y la acción «un paso más» (solo su descripción).
RETOUCHED: dict[str, tuple[str, ...]] = {"policy_presets": ("STANDARD", "HIGH", "MAXIMUM"), "risk_actions": ("STEP_UP",)}

_POLICY = sa.table("verification_policy", sa.column("flash_liveness"), sa.column("flash_paced"), schema=TENANCY)
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


def _base_texts() -> None:
    """Las descripciones en es-MX (las tablas del catálogo guardan el idioma base) desde `catalogs.json`."""
    seed = _seed("catalogs.json")
    for catalog, codes in RETOUCHED.items():
        table = sa.table(catalog, sa.column("code"), sa.column("description"), schema=CATALOG)
        for row in seed[catalog]:
            if row["code"] in codes:
                op.execute(table.update().where(table.c.code == row["code"]).values(description=row["description"]))


def _translations() -> None:
    """Las descripciones en cada idioma con archivo de traducciones (`catalogs.<idioma>.json`)."""
    rows: list[dict[str, str]] = []
    for path in sorted(SEED_DIR.glob("catalogs.*.json")):
        locale = path.name.removeprefix("catalogs.").removesuffix(".json")
        texts = json.loads(path.read_text(encoding="utf-8"))
        for catalog, codes in RETOUCHED.items():
            for code in codes:
                description = texts.get(catalog, {}).get(code, {}).get("description")
                if description:
                    rows.append(
                        {"catalog": catalog, "code": code, "locale": locale, "field": "description", "text": description}
                    )
    if not rows:
        return
    statement = postgresql.insert(_TRANSLATIONS).values(rows)
    op.execute(
        statement.on_conflict_do_update(
            index_elements=["catalog", "code", "locale", "field"], set_={"text": statement.excluded.text}
        )
    )


def upgrade() -> None:
    op.execute(_POLICY.update().values(flash_liveness="OFF", flash_paced=False))
    op.alter_column("verification_policy", "flash_liveness", server_default="OFF", schema=TENANCY)
    op.alter_column("verification_policy", "flash_paced", server_default=sa.false(), schema=TENANCY)
    _base_texts()
    _translations()


def downgrade() -> None:
    op.alter_column("verification_policy", "flash_liveness", server_default="OBSERVE", schema=TENANCY)
    op.alter_column("verification_policy", "flash_paced", server_default=sa.true(), schema=TENANCY)
