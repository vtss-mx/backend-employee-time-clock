"""Borrar de la base de datos el almacenamiento legado de imágenes (columna `photo_encrypted` y tabla `payment_receipts`)

Decisión del dueño del producto: ninguna imagen ni archivo se guarda en la base de datos, por ninguna
razón; van cifrados al bucket y la BD solo guarda su referencia (`0051`, README "Almacenamiento de
imágenes"). Las 4 fotos de registro facial que aún vivían aquí ya se llevaron al bucket y se verificaron
(`storage migrate-existing`, retirado en este mismo cambio), y no había ningún comprobante en
`billing.payment_receipts`. El dueño confirmó borrar este almacenamiento legado. Con él se van:

- `biometrics.face_enrollments.photo_encrypted` y su índice parcial `ix_face_enrollments_legacy_photo`
  (solo servía para encontrar las fotos por migrar);
- `billing.payment_receipts` (los bytes de los comprobantes de antes del bucket).

El código ya no los conoce (modelo, lectura legada, migración al bucket y su comando): ninguna imagen
vuelve a escribirse aquí.

**Se niega a correr si alguna fila aún conserva bytes**: no se pierde ninguna imagen. Sin
`CONCURRENTLY` a propósito: borrar la columna toma de todos modos el candado exclusivo de la tabla (un
instante: en PostgreSQL es solo un cambio del catálogo) y el índice está vacío.

`downgrade` recrea la columna, el índice y la tabla VACÍOS: las imágenes siguen en el bucket con su
referencia.

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-05 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_INDEX = "ix_face_enrollments_legacy_photo"


def upgrade() -> None:
    bind = op.get_bind()
    photos = bind.execute(
        sa.text("SELECT count(*) FROM biometrics.face_enrollments WHERE photo_encrypted IS NOT NULL")
    ).scalar()
    receipts = bind.execute(sa.text("SELECT count(*) FROM billing.payment_receipts")).scalar()
    if photos or receipts:
        raise RuntimeError(
            f"Aún hay imágenes en la base de datos ({photos} foto(s) de registro facial, {receipts} "
            "comprobante(s)): hay que llevarlas al bucket antes de esta migración (la versión 0052 tiene "
            "`python -m app.cli storage migrate-existing`). No se borra nada."
        )
    op.drop_index(LEGACY_INDEX, table_name="face_enrollments", schema="biometrics")
    with op.batch_alter_table("face_enrollments", schema="biometrics") as batch_op:
        batch_op.drop_column("photo_encrypted")
    op.drop_table("payment_receipts", schema="billing")


def downgrade() -> None:
    op.create_table(
        "payment_receipts",
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["billing.payments.id"],
            name=op.f("fk_payment_receipts_payment_id_payments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("payment_id", name=op.f("pk_payment_receipts")),
        schema="billing",
    )
    with op.batch_alter_table("face_enrollments", schema="biometrics") as batch_op:
        batch_op.add_column(sa.Column("photo_encrypted", sa.LargeBinary(), nullable=True))
    op.create_index(
        LEGACY_INDEX,
        "face_enrollments",
        ["id"],
        unique=False,
        schema="biometrics",
        postgresql_where=sa.text("photo_encrypted IS NOT NULL"),
    )
