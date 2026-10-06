"""Foto de perfil de cada persona: cifrada en el bucket, la BD solo guarda su referencia

Pedido del dueño del producto: "como empresa y como empleado debo poder actualizar mi foto de perfil... todo
debe guardarse de forma organizada en Firebase". Decisiones (detalle en el README, "Foto de perfil"):

- La foto es de la PERSONA (`auth.users`), no de un empleo: una persona trabaja en varias empresas con la misma
  cuenta. Todos los roles con "Mi perfil" (ADMIN, COMPANY, VALIDATOR, EMPLOYEE) suben, cambian o quitan la suya.
- `auth.user_avatars` (nueva): una fila por tamaño (512 y 96 px, WebP) con la referencia de su objeto CIFRADO en
  el bucket (`<prefijo>/people/users/<cuenta>/avatar/<versión>/<tamaño>.webp.enc`, solo ids): nombre, tipo,
  tamaño, SHA-256 y cuándo se subió. Ningún byte de la imagen en la BD (regla 13). Sin `company_id`: es de la
  plataforma (no lleva seguridad por fila). La llave primaria `(user_id, size_px)` sirve a sus dos consultas y a
  la FK `ON DELETE CASCADE` (§3.1.8): sin índices de más.
- `auth.users.avatar_version` (nueva, nula): la versión vigente, para que `/users/me`, el inicio de sesión y los
  listados de empleados armen la URL de la foto sin una consulta más (ya cargan la cuenta). Columna nula sin
  valor por omisión: en PostgreSQL agregarla es instantáneo (solo el catálogo) y las réplicas de la versión
  anterior la ignoran durante el despliegue gradual.

Comentarios (`alembic/sql/0061_comments.sql`): el de la tabla nueva y el del esquema `auth`.

`downgrade` encola en `ops.storage_deletions` los objetos de todas las fotos (lo que se borra de la BD nunca se
queda en la nube), quita la tabla y la columna y deja el comentario anterior del esquema.

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-07 10:00:00
"""

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTH = "auth"
OPS = "ops"
TABLE = "user_avatars"
#: Comentarios de la tabla nueva y del esquema (los mismos que ejecuta una base creada desde los modelos).
COMMENTS_FILE = Path(__file__).resolve().parents[1] / "sql" / "0061_comments.sql"
#: El comentario del esquema antes de esta migración (`0056_comments.sql`), para el `downgrade`.
PREVIOUS_AUTH_COMMENT = (
    "Identidad: cuentas, sesiones, cuentas recordadas y límites de peticiones (de la plataforma, sin seguridad por "
    "fila)."
)


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_version", sa.String(length=24), nullable=True), schema=AUTH)
    op.create_table(
        TABLE,
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("size_px", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.String(length=24), nullable=False),
        sa.Column("content_type", sa.String(length=30), nullable=False),
        sa.Column("object_name", sa.String(length=300), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size_px IN (96, 512)", name=op.f("ck_user_avatars_size_px")),
        sa.ForeignKeyConstraint(
            ["user_id"], [f"{AUTH}.users.id"], name=op.f("fk_user_avatars_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "size_px", name=op.f("pk_user_avatars")),
        schema=AUTH,
    )
    op.get_bind().exec_driver_sql(COMMENTS_FILE.read_text(encoding="utf-8").replace("%", "%%"))


def downgrade() -> None:
    op.execute(
        f"INSERT INTO {OPS}.storage_deletions (object_name, requested_at) "
        f"SELECT object_name, now() FROM {AUTH}.{TABLE} ON CONFLICT (object_name) DO NOTHING"
    )
    op.drop_table(TABLE, schema=AUTH)
    op.drop_column("users", "avatar_version", schema=AUTH)
    op.execute(f"COMMENT ON SCHEMA {AUTH} IS '{PREVIOUS_AUTH_COMMENT}'")
