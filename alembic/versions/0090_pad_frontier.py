"""PAD de frontera: más de 1000 rasgos de presentación en 7 familias, cada una una señal que nace en «Solo medir»

Decisión del dueño del producto (2026-10-07, «al menos 1000 señales de riesgo, I+D de frontera»; diseño en
docs/rd/pad-frontera-2026-10-08.md): en lugar de 1000 reglas que nieguen (y rechacen a personas reales), el servidor
MIDE más de 1000 rasgos REALES y enumerables de cada captura frontal (`facial_recognition/pad.py`) y los agrupa en 7
FAMILIAS; cada familia alimenta UNA señal del motor de riesgo que nace en «Solo medir» y NUNCA niega por sí sola hasta
que el dueño la calibre con datos reales (`risk_rules.CALIBRATING_SIGNALS`; invariante «lo nuevo nunca niega»).

- `ops.face_attempt_metrics` (particionada): columna `pad` (JSONB en PostgreSQL), nula y sin valor por omisión (pasa a
  todas sus particiones al instante). Guarda SOLO los 7 números por familia del intento (`{familia: 0-1}`), nunca los
  más de 1000 rasgos crudos ni imagen alguna (regla 13). La autocalibración de cada familia la lee de ahí
  (`face_security.SIGNALS`, «solo endurece»). Su patrón JSON es el de `tenancy.verification_policy.risk_signals`.
- Catálogos: 7 señales nuevas de `risk_signals` (familia `PRESENTATION`, modo `OBSERVE`, motivo `CAPTURE`) con sus
  traducciones a los seis idiomas con archivo (en-US, pt-BR, fr-FR, de-DE, it-IT, es-ES); su español vive en las
  columnas del seed.

Compatible con la versión anterior (agrega, no quita): columna nula y señales nuevas que el código viejo ignora. Nada
que depurar. `downgrade` quita la columna, las traducciones, las líneas base por empresa y las señales.

Revision ID: 0090
Revises: 0089
Create Date: 2026-10-08 12:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0090"
down_revision: str | None = "0089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
OPS = "ops"
NEW_SIGNALS = (
    "PAD_TEXTURE_ANOMALY",
    "PAD_FREQUENCY_ANOMALY",
    "PAD_COLOR_ANOMALY",
    "PAD_NOISE_ANOMALY",
    "PAD_SPECULAR_ANOMALY",
    "PAD_SHARPNESS_ANOMALY",
    "PAD_CHROMA_ANOMALY",
)

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


def _catalog() -> None:
    """Las señales nuevas (idempotente: una base nueva ya las cargó con el seed vigente) y sus traducciones."""
    seed = _seed("catalogs.json")
    items = [row for row in seed["risk_signals"] if row["code"] in NEW_SIGNALS]
    table = sa.table("risk_signals", *(sa.column(key) for key in items[0]), schema=CATALOG)
    op.execute(postgresql.insert(table).values(items).on_conflict_do_nothing(index_elements=["code"]))
    rows: list[dict[str, str]] = []
    for path in sorted(SEED_DIR.glob("catalogs.*.json")):
        locale = path.name.removeprefix("catalogs.").removesuffix(".json")
        texts = json.loads(path.read_text(encoding="utf-8"))
        rows += [
            {"catalog": "risk_signals", "code": code, "locale": locale, "field": field, "text": text}
            for code in NEW_SIGNALS
            for field, text in texts.get("risk_signals", {}).get(code, {}).items()
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
    # Tabla particionada: la columna nueva (nula, sin valor por omisión) pasa a todas sus particiones al instante.
    op.add_column(
        "face_attempt_metrics",
        sa.Column("pad", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True),
        schema=OPS,
    )
    _catalog()


def downgrade() -> None:
    op.execute(
        sa.delete(_TRANSLATIONS).where(
            _TRANSLATIONS.c.catalog == "risk_signals", _TRANSLATIONS.c.code.in_(NEW_SIGNALS)
        )
    )
    stats = sa.table("risk_signal_stats", sa.column("signal"), schema=OPS)
    op.execute(sa.delete(stats).where(stats.c.signal.in_(NEW_SIGNALS)))
    signals = sa.table("risk_signals", sa.column("code"), schema=CATALOG)
    op.execute(sa.delete(signals).where(signals.c.code.in_(NEW_SIGNALS)))
    op.drop_column("face_attempt_metrics", "pad", schema=OPS)
