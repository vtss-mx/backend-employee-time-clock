"""Cobro en varias monedas: pesos mexicanos, dólares y euros

Pedido del dueño del producto: "los costos se pueden generar en pesos mexicanos, dólares o euros... cuidado
con esto". Decisiones (detalle en el README, "Cobranza"):

- Catálogo nuevo `catalog.currencies` (código ISO 4217, nombre, símbolo y decimales): MXN, USD y EUR, las tres
  con 2 decimales. Los decimales viven en el catálogo para que el redondeo no dependa del código; como las
  columnas de dinero son NUMERIC(…, 2), el CHECK `decimals` los limita a 0..2.
- Cada empresa se cobra en la moneda de su plan: `billing.plans.currency` (ya existía como texto con 'MXN')
  pasa a ser una FK al catálogo. La moneda se cambia libremente hasta el primer cargo o pago de la empresa;
  después queda fija (lo valida el servicio con la empresa bloqueada: 422 `CURRENCY_LOCKED`).
- `billing.charges.currency` y `billing.payments.currency` (nuevas): cada cargo y cada pago guardan su moneda,
  así el historial se explica solo. Un pago debe ser en la moneda de la empresa (422 `CURRENCY_MISMATCH`) y
  el reparto de pagos solo cubre cargos de su misma moneda. Los totales de la plataforma se reportan por
  moneda: nunca se suman monedas distintas ni se convierten.
- Las filas que ya existen son de MXN (la única moneda que la API permitía hasta hoy): las columnas nuevas
  llegan con 'MXN' como valor por omisión, que además mantiene funcionando a las réplicas de la versión
  anterior durante el despliegue gradual (solo conocen pesos). El código nuevo siempre escribe la moneda.
- Las FK hacia el catálogo van sin índice (§3.1.8: un catálogo nunca se borra). Las de cargos y pagos se
  agregan `NOT VALID` y se validan después: `VALIDATE CONSTRAINT` recorre la tabla sin bloquear sus escrituras.
  La de planes (una fila por empresa) se agrega directo.
- Sin índices nuevos: las consultas que agrupan por moneda ya van por los índices de fecha o los parciales de
  abiertos y saldo a favor (la moneda se lee de la fila).

`downgrade` quita las columnas nuevas, la FK de planes y el catálogo (los planes conservan su texto 'MXN').
Una base con cargos o pagos en otra moneda pierde ese dato al bajar: nadie debe bajar de esta versión con
empresas cobradas en dólares o euros.

Revision ID: 0060
Revises: 0056
Create Date: 2026-10-06 10:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0060"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
BILLING = "billing"
#: Tablas de movimientos que guardan su moneda (con su FK validada aparte).
MOVEMENTS = ("charges", "payments")
DEFAULT_CURRENCY = "MXN"


def _fk(table: str) -> str:
    return f"fk_{table}_currency_currencies"


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    currencies = op.create_table(
        "currencies",
        sa.Column("symbol", sa.String(length=8), nullable=False),
        sa.Column("decimals", sa.SmallInteger(), server_default=sa.text("2"), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint("decimals BETWEEN 0 AND 2", name=op.f("ck_currencies_decimals")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_currencies")),
        schema="catalog",
    )
    op.bulk_insert(currencies, seed["currencies"])
    # Planes: una fila por empresa, todas con 'MXN' (la API no permitía otra): la FK se valida al crearse.
    op.create_foreign_key(
        op.f(_fk("plans")),
        "plans",
        "currencies",
        ["currency"],
        ["code"],
        source_schema=BILLING,
        referent_schema="catalog",
    )
    for table in MOVEMENTS:
        # Valor por omisión constante: PostgreSQL agrega la columna sin reescribir la tabla.
        op.add_column(
            table,
            sa.Column("currency", sa.String(length=3), server_default=DEFAULT_CURRENCY, nullable=False),
            schema=BILLING,
        )
        op.execute(
            f"ALTER TABLE {BILLING}.{table} ADD CONSTRAINT {_fk(table)} "
            "FOREIGN KEY (currency) REFERENCES catalog.currencies (code) NOT VALID"
        )
        op.execute(f"ALTER TABLE {BILLING}.{table} VALIDATE CONSTRAINT {_fk(table)}")


def downgrade() -> None:
    for table in reversed(MOVEMENTS):
        op.drop_constraint(op.f(_fk(table)), table, schema=BILLING, type_="foreignkey")
        op.drop_column(table, "currency", schema=BILLING)
    op.drop_constraint(op.f(_fk("plans")), "plans", schema=BILLING, type_="foreignkey")
    op.drop_table("currencies", schema="catalog")
