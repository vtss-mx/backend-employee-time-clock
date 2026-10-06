"""Registro facial con 36 fotos y consenso de identidad de la ráfaga (decisión del dueño del producto, 2026-10-06)

«Asegúrate de que se pidan varias fotos, por lo menos 36». El registro elige sus referencias entre 36 fotos completas
(`enrollment_selection`: no cambia ninguna tabla, solo cuántas fotos llegan) y la verificación usa 36 fotos ligeras (la
ráfaga de 36 recortes de 160 px). Con los mejores recortes quietos de la ráfaga se mide el CONSENSO de identidad del
video en vivo contra las muestras de la persona (`identity_core.burst_rejects`): solo endurece; bajo lo exigido menos
`FACE_CONSENSUS_MARGIN`, el intento es "Rostro no reconocido" aunque las frontales coincidan.

- `ops.face_attempt_metrics` (particionada): `burst_consensus`, nula y sin valor por omisión (pasa a todas sus
  particiones al instante, sin reescribir la tabla), para calibrar el margen con intentos reales. Solo un número.
- Su comentario (`alembic/sql/0073_comments.sql`).

Ningún catálogo ni texto cambia: el consenso es parte de la coincidencia (no es una señal del motor de riesgo) y el
registro usa los motivos de `face_errors` que ya existen. Compatible con la versión anterior en marcha (una columna nula
que esa versión no escribe). `downgrade` quita la columna.

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-06 06:00:00
"""

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
OPS = "ops"


def upgrade() -> None:
    # Tabla particionada: la columna nueva (nula, sin valor por omisión) pasa a todas sus particiones al instante.
    op.add_column("face_attempt_metrics", sa.Column("burst_consensus", sa.Float(), nullable=True), schema=OPS)
    op.get_bind().exec_driver_sql((SQL_DIR / "0073_comments.sql").read_text(encoding="utf-8").replace("%", "%%"))


def downgrade() -> None:
    op.drop_column("face_attempt_metrics", "burst_consensus", schema=OPS)
