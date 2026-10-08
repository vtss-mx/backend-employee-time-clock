"""Lentes permitidos y prueba de vida completa del registro (decisión del dueño del producto, 2026-10-07)

El dueño probó el registro facial en escritorio con lentes y dio dos decisiones firmes:

1. **Los lentes se permiten.** Aparecía la insignia «Lentes» sobre el rostro y el texto «Quítate los lentes para
   continuar»: «ese mensaje hay que quitarlo por completo». La regla `block_glasses` se RETIRA como se retiró el
   destello (migración `0080`): queda en `false` en TODA política existente y nace apagada en las nuevas; el servidor
   nunca rechaza por lentes aunque la columna diga `true` (`PolicySnapshot.face_policy`: OFF es OFF en todas partes) y
   el control del ADMIN se muestra apagado y sin cambios con la nota «Desactivado por decisión del producto
   (2026-10-07)». El detector de accesorios (CLIP, `FACE_GLASSES_THRESHOLD`) se conserva con sus pruebas, que lo
   encienden a propósito. Cubrebocas y gorra (`block_mask`, `block_headwear`) siguen siendo reglas del servidor.

2. **Prueba de vida completa en el registro.** «Se debe pedir prueba de vida: que mueva su cabeza a la derecha,
   izquierda, arriba, abajo y que centre su cara.» El reto del registro (propio y en persona) pide SIEMPRE los cuatro
   movimientos (`liveness_service.ENROLLMENT_ACTIONS`, en orden al azar) y entre uno y otro la vuelta al frente; la
   verificación sigue con los 1 a 3 movimientos de la política (`liveness_steps`, cuyo CHECK no cambia). La tabla
   `biometrics.face_challenges` guardaba hasta tres movimientos (`direction`, `second_direction`, `third_direction`):
   se agrega `fourth_direction`, nula, con la misma FK al catálogo `liveness_actions` (los retos de 1 a 3 pasos la
   dejan en NULL; sin índice: la tabla se lee solo por su llave y se depura por `expires_at`).

Compatible con la versión anterior en marcha (solo agrega una columna nula y cambia datos y un valor por omisión).
`downgrade` quita la columna (un reto del registro en curso vence solo) y regresa el valor por omisión anterior de
`block_glasses`; las políticas quedan como están: volver a exigir retirar los lentes en una empresa sería una decisión
explícita del ADMIN por la API, no de una migración.

Revision ID: 0082
Revises: 0081
Create Date: 2026-10-07 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANCY = "tenancy"
BIOMETRICS = "biometrics"
CATALOG = "catalog"

_POLICY = sa.table("verification_policy", sa.column("block_glasses"), schema=TENANCY)


def upgrade() -> None:
    # 1. Lentes permitidos: en toda empresa y por omisión en las nuevas.
    op.execute(_POLICY.update().values(block_glasses=False))
    op.alter_column("verification_policy", "block_glasses", server_default=sa.false(), schema=TENANCY)
    # 2. El cuarto movimiento del reto del registro.
    op.add_column(
        "face_challenges", sa.Column("fourth_direction", sa.String(length=20), nullable=True), schema=BIOMETRICS
    )
    op.create_foreign_key(
        op.f("fk_face_challenges_fourth_direction_liveness_actions"),
        "face_challenges",
        "liveness_actions",
        ["fourth_direction"],
        ["code"],
        source_schema=BIOMETRICS,
        referent_schema=CATALOG,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_face_challenges_fourth_direction_liveness_actions"),
        "face_challenges",
        schema=BIOMETRICS,
        type_="foreignkey",
    )
    op.drop_column("face_challenges", "fourth_direction", schema=BIOMETRICS)
    op.alter_column("verification_policy", "block_glasses", server_default=None, schema=TENANCY)
