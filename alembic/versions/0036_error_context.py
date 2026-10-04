"""Contexto literal de cada ocurrencia de un error

`ops.error_occurrences.context` (JSON): quién (cuenta, correo, rol, empresa), la petición (método,
URL, encabezados, cuerpo) y la respuesta (estado y cuerpo), tal cual, para que el ADMIN resuelva
con el contexto completo. Nunca guarda secretos (contraseñas, tokens, llaves, cookies) ni archivos
(de las fotos solo su nombre, tipo y tamaño): lo garantiza `app/core/error_context.py`.

No modifica datos existentes (las ocurrencias anteriores quedan sin contexto).

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-04 06:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("error_occurrences", sa.Column("context", sa.Text(), nullable=True), schema="ops")


def downgrade() -> None:
    op.drop_column("error_occurrences", "context", schema="ops")
