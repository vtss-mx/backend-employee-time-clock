"""Imágenes y archivos: cifrados en el bucket de Google Cloud Storage, nunca en la base de datos

Decisión del dueño del producto: "por ninguna razón se guardan imágenes en nuestra base de datos". La foto
de referencia del registro facial y el comprobante de un pago (y cualquier imagen o archivo futuro) se
cifran con la llave de la plataforma (`DATA_ENCRYPTION_KEY`) y se suben DURANTE la petición al bucket
privado `gs://employee-time-clock-fb8ba.firebasestorage.app`, organizado por empresa; la BD solo guarda la
referencia. Detalle en el README, "Almacenamiento de imágenes".

- `biometrics.face_enrollments`: `photo_object` (nombre del objeto), `photo_size` (tamaño de la imagen),
  `photo_sha256` (del objeto cifrado: se verifica al leerlo) y `photo_uploaded_at`.
- `billing.payments`: `receipt_object`, `receipt_sha256` y `receipt_uploaded_at` (el nombre, tipo y
  tamaño del comprobante ya estaban en el pago).
- `ops.storage_deletions`: objetos por borrar del bucket (registro rechazado, empleado borrado, objeto de
  una transacción que no se confirmó). La escribe la misma transacción y la vacía el mantenimiento.
- `ops.storage_status`: resultado de la última vuelta de cada tarea con el bucket (borrar, migrar lo
  existente) para el estado del servidor del ADMIN, el mismo en todas las instancias.
- Índice parcial `ix_face_enrollments_legacy_photo` (`photo_encrypted IS NOT NULL`): las fotos que aún
  viven en la BD, para `python -m app.cli storage migrate-existing` y el estado del ADMIN. Nada nuevo
  entra ahí. `CONCURRENTLY`: la tabla crece con cada registro facial y no se bloquean sus escrituras.

Lo existente NO se toca aquí: `photo_encrypted` y `billing.payment_receipts` siguen (solo lectura) hasta
que `storage migrate-existing` (requiere la cuenta de servicio) lleve cada imagen al bucket y la quite;
después se aplica la migración que borra esa columna y esa tabla (hoy en `alembic/pending/`, que se niega
a correr si alguna fila conserva bytes). Orden: configurar la llave → migrate-existing → esa migración.

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-05 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIOMETRICS = "biometrics"
BILLING = "billing"
OPS = "ops"
LEGACY_INDEX = "ix_face_enrollments_legacy_photo"


def upgrade() -> None:
    with op.batch_alter_table("face_enrollments", schema=BIOMETRICS) as batch_op:
        batch_op.add_column(sa.Column("photo_object", sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column("photo_size", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("photo_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("photo_uploaded_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("payments", schema=BILLING) as batch_op:
        batch_op.add_column(sa.Column("receipt_object", sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column("receipt_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("receipt_uploaded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "storage_deletions",
        sa.Column("object_name", sa.String(length=300), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("object_name", name=op.f("pk_storage_deletions")),
        schema=OPS,
    )
    op.create_table(
        "storage_status",
        sa.Column("task", sa.String(length=40), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("task", name=op.f("pk_storage_status")),
        schema=OPS,
    )
    # CONCURRENTLY no puede ir dentro de una transacción. Repetible tras una falla a la mitad (un índice
    # inválido de un intento anterior se borra antes de crearlo).
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {BIOMETRICS}.{LEGACY_INDEX}")
        op.create_index(
            LEGACY_INDEX,
            "face_enrollments",
            ["id"],
            unique=False,
            schema=BIOMETRICS,
            postgresql_where=sa.text("photo_encrypted IS NOT NULL"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    # Volver atrás borraría las referencias: las imágenes ya no estarían en ninguna parte de la BD (solo en
    # el bucket, sin dueño). Se niega mientras alguna fila tenga su imagen en el bucket.
    bind = op.get_bind()
    referenced = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM biometrics.face_enrollments WHERE photo_object IS NOT NULL)"
            " + (SELECT count(*) FROM billing.payments WHERE receipt_object IS NOT NULL)"
        )
    ).scalar()
    if referenced:
        raise RuntimeError(
            f"{referenced} imagen(es) viven en el bucket y su única referencia está en estas columnas: "
            "no se puede volver a 0050 sin perderlas."
        )
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {BIOMETRICS}.{LEGACY_INDEX}")
    op.drop_table("storage_status", schema=OPS)
    op.drop_table("storage_deletions", schema=OPS)
    with op.batch_alter_table("payments", schema=BILLING) as batch_op:
        for column in ("receipt_uploaded_at", "receipt_sha256", "receipt_object"):
            batch_op.drop_column(column)
    with op.batch_alter_table("face_enrollments", schema=BIOMETRICS) as batch_op:
        for column in ("photo_uploaded_at", "photo_sha256", "photo_size", "photo_object"):
            batch_op.drop_column(column)
