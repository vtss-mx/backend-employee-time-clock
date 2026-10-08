"""Movimientos de la prueba de vida configurables por empresa (decisión del dueño del producto, 2026-10-08)

Voltear la cabeza hacia arriba o abajo se le dificulta a algunas personas, así que cada empresa elige qué movimientos de
cabeza puede pedir la prueba de vida. Por omisión solo los giros (derecha e izquierda); mirar arriba y abajo nacen
apagados. Siempre deben quedar AL MENOS DOS activos (para que el reto tenga de dónde elegir sin repetir el mismo
seguido): CHECK `liveness_moves_min`. Reemplaza la decisión del 2026-10-07 («la prueba de vida del registro es completa,
los cuatro movimientos»): ahora el registro pide los que la empresa habilitó.

- `tenancy.verification_policy.enable_turn_right` / `enable_turn_left` (nuevos, ENCENDIDOS por omisión).
- `tenancy.verification_policy.enable_look_up` / `enable_look_down` (nuevos, APAGADOS por omisión).
- CHECK `liveness_moves_min`: la suma de los cuatro ≥ 2.

Compatible con la versión anterior en marcha: columnas con valor por omisión. Sin cambios en `face_challenges` (sus
cuatro columnas de dirección ya caben todos los movimientos de cabeza). `downgrade` quita el CHECK y las columnas.

Revision ID: 0089
Revises: 0088
Create Date: 2026-10-08 10:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0089"
down_revision: str | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANCY = "tenancy"
TABLE = "verification_policy"
#: La misma SQL que el modelo (`CheckConstraint liveness_moves_min`): al menos dos movimientos de cabeza activos.
_MIN_MOVES = (
    "(CAST(enable_turn_right AS INTEGER) + CAST(enable_turn_left AS INTEGER) "
    "+ CAST(enable_look_up AS INTEGER) + CAST(enable_look_down AS INTEGER)) >= 2"
)


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("enable_turn_right", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        schema=TENANCY,
    )
    op.add_column(
        TABLE,
        sa.Column("enable_turn_left", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        schema=TENANCY,
    )
    op.add_column(
        TABLE,
        sa.Column("enable_look_up", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        schema=TENANCY,
    )
    op.add_column(
        TABLE,
        sa.Column("enable_look_down", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        schema=TENANCY,
    )
    op.create_check_constraint("liveness_moves_min", TABLE, _MIN_MOVES, schema=TENANCY)


def downgrade() -> None:
    op.drop_constraint("liveness_moves_min", TABLE, type_="check", schema=TENANCY)
    op.drop_column(TABLE, "enable_look_down", schema=TENANCY)
    op.drop_column(TABLE, "enable_look_up", schema=TENANCY)
    op.drop_column(TABLE, "enable_turn_left", schema=TENANCY)
    op.drop_column(TABLE, "enable_turn_right", schema=TENANCY)
