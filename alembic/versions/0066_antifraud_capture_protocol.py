"""Antifraude de identidad, fase 2a: protocolo de captura de frontera (ráfaga de recortes y destello dictado por el
servidor) y sus señales físicas

Lo primero de la fase 2 de `docs/rd/antifraude-identidad.md` (§2.2, §2.3 y §7.1), con la decisión D11 del dueño del
producto (una ráfaga corta de recortes del rostro de ~0.1-0.15 MB por intento). La inyección deja de poder fabricarse
"sin prisa": los colores del destello se revelan uno por uno por el canal en vivo con tiempo por color, y la ráfaga
deja medir continuidad, micromovimiento y pulso. Toda señal nueva nace en «Solo medir» y ninguna niega
(`risk_rules.ASK_ONLY_SIGNALS`; el pulso, además, nunca decide: `MEASURE_ONLY_SIGNALS`).

- `tenancy.verification_policy`: `flash_paced` (destello dictado) y `capture_burst` (ráfaga), encendidos: miden desde el
  primer día y no bloquean a nadie (una fila por empresa: el valor constante no reescribe la tabla).
- `biometrics.face_challenges`: `flash_paced` (el reto NO entregó sus colores en claro; tabla pequeña, una fila por
  cuenta con reto vigente).
- `ops.face_attempt_metrics` (particionada): los números del protocolo, nulos y sin valor por omisión (pasan a todas sus
  particiones al instante): `burst_frames`, `burst_motion`, `pulse_snr`, `moire`, `noise_ratio`, `parallax`,
  `flash_pace_ms`. Solo números: la hoja de la ráfaga se analiza en memoria y se descarta.
- `ops.fraud_evidence`: la hoja de la ráfaga puede ir como evidencia de un caso (kind `BURST`) por el MISMO camino de la
  decisión D1 (cifrada en el bucket, solo de intentos sospechosos). El CHECK nuevo se agrega `NOT VALID` y se valida
  después (no frena escrituras).
- Catálogos: 11 señales nuevas del motor de riesgo (`risk_signals`) con su traducción a en-US y la descripción del nivel
  Máximo al día (sin destello dictado o sin ráfaga pide un paso más), en los dos idiomas.

`downgrade` deja todo como estaba (columnas, CHECK, señales con su línea base y sus traducciones, y el texto anterior
del nivel Máximo en los dos idiomas).

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-05 18:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
CATALOG = "catalog"
TENANCY = "tenancy"
BIOMETRICS = "biometrics"
OPS = "ops"
LOCALE = "en-US"
NEW_SIGNALS = (
    "BURST_MISSING",
    "BURST_DISCONTINUOUS",
    "BURST_FROZEN",
    "BURST_LOOP",
    "MOIRE_HIGH",
    "NOISE_MISMATCH",
    "PERSPECTIVE_FLAT",
    "PULSE_ABSENT",
    "FLASH_UNPACED",
    "FLASH_PACE_TIMING",
    "FLASH_PACE_MISMATCH",
)
METRIC_COLUMNS = (
    ("burst_frames", sa.SmallInteger()),
    ("burst_motion", sa.Float()),
    ("pulse_snr", sa.Float()),
    ("moire", sa.Float()),
    ("noise_ratio", sa.Float()),
    ("parallax", sa.Float()),
    ("flash_pace_ms", sa.Float()),
)
EVIDENCE_CHECK = "ck_fraud_evidence_kind"
EVIDENCE_KINDS_BEFORE = "kind IN ('FRONTAL', 'STEP', 'FLASH')"
EVIDENCE_KINDS_AFTER = "kind IN ('FRONTAL', 'STEP', 'FLASH', 'BURST')"
#: Textos que cambian: (catálogo, código, columna) → (español anterior, inglés anterior). El nuevo sale del seed.
PREVIOUS_TEXTS = {
    ("policy_presets", "MAXIMUM", "description"): (
        "Para sitios con fraude confirmado: tres movimientos en 30 s, anti-spoofing Máximo, reglas duras obligatorias, "
        "la empresa aprueba cada dispositivo nuevo y el riesgo alto se niega. Más reintentos con poca luz.",
        "For sites with confirmed fraud: three movements in 30 s, Maximum anti-spoofing, required hard rules, the "
        "company approves each new device, and high risk is denied. More retries in low light.",
    ),
}
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


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _catalog(seed: dict, english: dict) -> None:
    """Las señales nuevas (idempotente: una base nueva ya las cargó con el seed vigente), sus traducciones y el texto
    del nivel Máximo al día en los dos idiomas."""
    items = [row for row in seed["risk_signals"] if row["code"] in NEW_SIGNALS]
    table = sa.table("risk_signals", *(sa.column(key) for key in items[0]), schema=CATALOG)
    op.execute(postgresql.insert(table).values(items).on_conflict_do_nothing(index_elements=["code"]))
    bind = op.get_bind()
    for catalog, code, column in PREVIOUS_TEXTS:
        text = next(row[column] for row in seed[catalog] if row["code"] == code)
        bind.execute(
            sa.text(f"UPDATE {CATALOG}.{catalog} SET {column} = :text WHERE code = :code"), {"text": text, "code": code}
        )
    rows = [
        {"catalog": "risk_signals", "code": code, "locale": LOCALE, "field": field, "text": text}
        for code in NEW_SIGNALS
        for field, text in english["risk_signals"][code].items()
    ]
    rows += [
        {"catalog": catalog, "code": code, "locale": LOCALE, "field": column, "text": english[catalog][code][column]}
        for catalog, code, column in PREVIOUS_TEXTS
    ]
    statement = postgresql.insert(_TRANSLATIONS).values(rows)
    op.execute(
        statement.on_conflict_do_update(
            index_elements=["catalog", "code", "locale", "field"], set_={"text": statement.excluded.text}
        )
    )


def upgrade() -> None:
    _catalog(_seed("catalogs.json"), _seed(f"catalogs.{LOCALE}.json"))
    for name in ("flash_paced", "capture_burst"):
        op.add_column(
            "verification_policy",
            sa.Column(name, sa.Boolean(), server_default=sa.text("true"), nullable=False),
            schema=TENANCY,
        )
    op.add_column(
        "face_challenges",
        sa.Column("flash_paced", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        schema=BIOMETRICS,
    )
    # Tabla particionada: cada columna nueva (nula, sin valor por omisión) pasa a todas sus particiones al instante.
    for name, kind in METRIC_COLUMNS:
        op.add_column("face_attempt_metrics", sa.Column(name, kind, nullable=True), schema=OPS)
    op.execute(f"ALTER TABLE {OPS}.fraud_evidence DROP CONSTRAINT IF EXISTS {EVIDENCE_CHECK}")
    op.execute(
        f"ALTER TABLE {OPS}.fraud_evidence ADD CONSTRAINT {EVIDENCE_CHECK} CHECK ({EVIDENCE_KINDS_AFTER}) NOT VALID"
    )
    op.execute(f"ALTER TABLE {OPS}.fraud_evidence VALIDATE CONSTRAINT {EVIDENCE_CHECK}")
    op.get_bind().exec_driver_sql((SQL_DIR / "0066_comments.sql").read_text(encoding="utf-8").replace("%", "%%"))


def downgrade() -> None:
    # Nada se borra al regresar: la evidencia de ráfagas ya guardada se queda hasta que la purgue su retención (el CHECK
    # anterior vuelve `NOT VALID`: no revisa esas filas, sí todo lo nuevo).
    op.execute(f"ALTER TABLE {OPS}.fraud_evidence DROP CONSTRAINT IF EXISTS {EVIDENCE_CHECK}")
    op.execute(
        f"ALTER TABLE {OPS}.fraud_evidence ADD CONSTRAINT {EVIDENCE_CHECK} CHECK ({EVIDENCE_KINDS_BEFORE}) NOT VALID"
    )
    for name, _ in reversed(METRIC_COLUMNS):
        op.drop_column("face_attempt_metrics", name, schema=OPS)
    op.drop_column("face_challenges", "flash_paced", schema=BIOMETRICS)
    for name in ("capture_burst", "flash_paced"):
        op.drop_column("verification_policy", name, schema=TENANCY)
    bind = op.get_bind()
    for (catalog, code, column), (spanish, english) in PREVIOUS_TEXTS.items():
        bind.execute(
            sa.text(f"UPDATE {CATALOG}.{catalog} SET {column} = :text WHERE code = :code"),
            {"text": spanish, "code": code},
        )
        bind.execute(
            sa.text(
                f"UPDATE {CATALOG}.translations SET text = :text "
                "WHERE catalog = :catalog AND code = :code AND locale = :locale AND field = :field"
            ),
            {"text": english, "catalog": catalog, "code": code, "locale": LOCALE, "field": column},
        )
    op.execute(f"DELETE FROM {CATALOG}.translations WHERE catalog = 'risk_signals' AND code IN ({_in(NEW_SIGNALS)})")
    op.execute(f"DELETE FROM {OPS}.risk_signal_stats WHERE signal IN ({_in(NEW_SIGNALS)})")
    op.execute(f"DELETE FROM {CATALOG}.risk_signals WHERE code IN ({_in(NEW_SIGNALS)})")
