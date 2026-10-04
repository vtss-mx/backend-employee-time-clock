"""Política de verificación: confianza propia para identificar (1:N) y calidad mínima de la captura

Resultado del R&D de la política (estado del arte en verificación facial):
- `identify_confidence`: identificar a alguien entre TODA la plantilla (1:N, validadores) multiplica
  las oportunidades de una coincidencia falsa respecto a verificar a una sola persona (1:1); NIST
  recomienda umbrales más estrictos para 1:N. Nivel del catálogo `confidence_levels` (99.999 % por
  omisión); nunca se aplica por debajo de `min_confidence`.
- `min_capture_quality`: calidad mínima de cada captura (detección, nitidez y luz combinadas, en el
  espíritu de ISO/IEC 29794-5): una imagen pobre compara mal y facilita los engaños. 0.40 por omisión
  (solo descarta capturas muy pobres); 0 = sin mínimo.
- Catálogo `face_errors`: `LOW_QUALITY` (el mensaje para la persona).

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-04 18:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
TABLE = "verification_policy"
SCHEMA = "tenancy"
FK = "fk_verification_policy_identify_confidence_confidence_levels"
INDEX = "ix_verification_policy_identify_confidence"
CHECK = "ck_verification_policy_min_capture_quality"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("identify_confidence", sa.Numeric(7, 5), server_default=sa.text("0.99999"), nullable=False),
        schema=SCHEMA,
    )
    op.add_column(
        TABLE,
        sa.Column("min_capture_quality", sa.Numeric(3, 2), server_default=sa.text("0.4"), nullable=False),
        schema=SCHEMA,
    )
    # La confianza 1:N arranca igual que la 1:1 de cada empresa (nadie queda menos protegido).
    op.execute(f"UPDATE {SCHEMA}.{TABLE} SET identify_confidence = min_confidence")
    op.create_foreign_key(
        FK,
        TABLE,
        "confidence_levels",
        ["identify_confidence"],
        ["value"],
        source_schema=SCHEMA,
        referent_schema="catalog",
    )
    op.create_index(INDEX, TABLE, ["identify_confidence"], schema=SCHEMA)
    op.create_check_constraint(CHECK, TABLE, "min_capture_quality BETWEEN 0 AND 0.9", schema=SCHEMA)
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    for row in (r for r in seed["face_errors"] if r["code"] == "LOW_QUALITY"):
        table = sa.table("face_errors", *(sa.column(key) for key in row), schema="catalog")
        op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=["code"]))


def downgrade() -> None:
    errors = sa.table("face_errors", sa.column("code"), schema="catalog")
    op.execute(sa.delete(errors).where(errors.c.code == "LOW_QUALITY"))
    op.drop_constraint(CHECK, TABLE, schema=SCHEMA, type_="check")
    op.drop_index(INDEX, table_name=TABLE, schema=SCHEMA)
    op.drop_constraint(FK, TABLE, schema=SCHEMA, type_="foreignkey")
    op.drop_column(TABLE, "min_capture_quality", schema=SCHEMA)
    op.drop_column(TABLE, "identify_confidence", schema=SCHEMA)
