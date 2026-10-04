"""Reconocimiento facial evolutivo: cada empleado aprende de sus identificaciones seguras

Hasta ahora cada empleado se comparaba solo con las muestras de su registro aprobado: si cambiaba la
luz del acceso, la cámara del validador, el peinado, la barba o simplemente pasaba el tiempo, la
confianza bajaba y había que repetir capturas o volver a registrarse.

Ahora la galería de cada empleado evoluciona con el uso (`app/services/face_learning.py`):

- `biometrics.face_embeddings.learned`: muestra aprendida de una identificación (no la aprobó una
  persona). Las del registro aprobado (`learned = false`) son el ancla: nunca se reemplazan y toda
  muestra nueva debe parecerse a ellas, para que la galería no se desvíe hacia otra persona.
- `matches` y `last_matched_at`: veces que la muestra fue la más parecida en una identificación
  exitosa y cuándo fue la última. Es su utilidad: al llenarse los lugares, deja el suyo la muestra
  aprendida que lleva más tiempo sin servir.
- `tenancy.verification_policy.adaptive_learning`: cada empresa decide si su galería aprende
  (activo por defecto).

El mantenimiento retira las muestras aprendidas que llevan FACE_LEARNING_STALE_DAYS sin servir
(índice parcial por su último momento útil): la galería se renueva sola con el paso del tiempo.

Las muestras que antes se agregaban tras una verificación (sin registro de origen) se marcan como
aprendidas: así compiten por su lugar como las nuevas. No se borra ningún dato.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-03 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDINGS = {"table_name": "face_embeddings", "schema": "biometrics"}


def upgrade() -> None:
    op.add_column(column=sa.Column("learned", sa.Boolean(), server_default=sa.false(), nullable=False), **EMBEDDINGS)
    op.add_column(column=sa.Column("matches", sa.Integer(), server_default=sa.text("0"), nullable=False), **EMBEDDINGS)
    op.add_column(column=sa.Column("last_matched_at", sa.DateTime(timezone=True), nullable=True), **EMBEDDINGS)
    op.create_check_constraint(op.f("ck_face_embeddings_matches_non_negative"), condition="matches >= 0", **EMBEDDINGS)
    op.execute("UPDATE biometrics.face_embeddings SET learned = true WHERE enrollment_id IS NULL")
    op.create_index(
        "ix_face_embeddings_learned_last_useful",
        "face_embeddings",
        [sa.text("coalesce(last_matched_at, created_at)")],
        schema="biometrics",
        postgresql_where=sa.text("learned"),
    )
    op.add_column(
        "verification_policy",
        sa.Column("adaptive_learning", sa.Boolean(), server_default=sa.true(), nullable=False),
        schema="tenancy",
    )


def downgrade() -> None:
    op.drop_column("verification_policy", "adaptive_learning", schema="tenancy")
    op.drop_index("ix_face_embeddings_learned_last_useful", table_name="face_embeddings", schema="biometrics")
    op.drop_constraint(op.f("ck_face_embeddings_matches_non_negative"), type_="check", **EMBEDDINGS)
    for column in ("last_matched_at", "matches", "learned"):
        op.drop_column(column_name=column, **EMBEDDINGS)
