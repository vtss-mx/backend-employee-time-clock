"""Depuración de datos que ya no sirven (confirmada por el dueño del producto)

- **QR fijos anteriores**: los QR impresos de la versión anterior ya no son válidos (los QR son
  dinámicos y de un solo uso desde 0028). Se borran sus filas y la columna `token_encrypted` que solo
  ellos usaban. Quien escanee uno impreso sigue viendo el motivo `STATIC_QR` (se reconoce por su
  prefijo, no por la fila).
- **Errores 4xx guardados antes de 0044**: con la política nueva un 4xx ya no se registra (es un
  resultado normal); las filas WARNING que quedaron son ruido en la bandeja del ADMIN. Se borran con
  sus ocurrencias (ON DELETE CASCADE).

Borrado definitivo, confirmado por el dueño el 2026-10-04. El downgrade restaura la columna (vacía);
las filas borradas no se recuperan.

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-05 06:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    qr = sa.table("employee_qr_codes", sa.column("token_encrypted"), schema="workforce")
    op.execute(sa.delete(qr).where(qr.c.token_encrypted.is_not(None)))
    op.drop_column("employee_qr_codes", "token_encrypted", schema="workforce")
    reports = sa.table("error_reports", sa.column("severity"), schema="ops")
    op.execute(sa.delete(reports).where(reports.c.severity == "WARNING"))


def downgrade() -> None:
    op.add_column(
        "employee_qr_codes", sa.Column("token_encrypted", sa.LargeBinary(), nullable=True), schema="workforce"
    )
