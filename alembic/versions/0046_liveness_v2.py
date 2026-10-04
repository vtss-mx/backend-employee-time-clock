"""Prueba de vida reforzada y seguridad facial que se mejora sola

Decisión del dueño del producto: que el reconocimiento facial sea lo más difícil de engañar posible,
también por una IA, y que la plataforma se endurezca sola con lo que mide.

- Más movimientos al azar en el reto: además de girar, mirar arriba o abajo y acercarse
  (`catalog.liveness_actions`), hasta tres por reto (`verification_policy.liveness_steps` 1..3): un
  video grabado o generado de antemano tiene que acertar una secuencia mucho más larga.
- Tiempo límite del reto por empresa (`liveness_timeout_seconds`, 60 s por omisión): menos margen
  para fabricar la respuesta.
- Destello de colores (reto fotométrico): `catalog.flash_modes` y `verification_policy.flash_liveness`.
  Las empresas quedan en OBSERVE (se mide sin bloquear) hasta calibrarlo con capturas reales.
- `biometrics.face_challenges` guarda el tercer movimiento, los colores y cuándo se emitió (el tiempo
  humano mínimo se mide desde ahí, no desde una duración fija).
- `ops.face_attempt_metrics`: los números de cada intento facial (nunca imágenes ni plantillas) y
  `ops.security_thresholds`: los umbrales que la plataforma ajustó sola (solo endurece).
- Motivos y mensajes nuevos (FLASH_MISMATCH, FLASH_INCONCLUSIVE); la prueba de vida habla de
  "movimiento" en vez de "giro".
- Pantalla del ADMIN "Seguridad facial" en el módulo Operación.

Una base nueva ya tiene las filas del seed (0020/0021/0043 lo copian): aquí se insertan solo si faltan.
No modifica datos de las empresas.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-05 02:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
ACTIONS = ("LOOK_UP", "LOOK_DOWN", "MOVE_CLOSER")
FACE_ERRORS = ("FLASH_MISMATCH",)
REASONS = ("FLASH_MISMATCH", "FLASH_INCONCLUSIVE")
SCREEN = "ADMIN_FACE_SECURITY"
#: Mensajes de LIVENESS_FAILED antes de esta migración (para revertir).
PREVIOUS_MESSAGES = (
    ("verification_reasons", "No fue posible verificar tu identidad: no se detectó el giro de cabeza solicitado"),
    ("face_errors", "No se detectó el giro de cabeza solicitado. Inténtalo de nuevo"),
)
STEPS_CHECK = "ck_verification_policy_liveness_steps"
TIMEOUT_CHECK = "ck_verification_policy_liveness_timeout_seconds"


def _insert_missing(table_name: str, row: dict, keys: list[str]) -> None:
    table = sa.table(table_name, *(sa.column(key) for key in row), schema="catalog")
    op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=keys))


def _set_message(table_name: str, code: str, message: str) -> None:
    table = sa.table(table_name, sa.column("code"), sa.column("message"), schema="catalog")
    op.execute(sa.update(table).where(table.c.code == code).values(message=message))


def _catalogs(seed: dict) -> None:
    modes = op.create_table(
        "flash_modes",
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_flash_modes")),
        schema="catalog",
    )
    op.bulk_insert(modes, seed["flash_modes"])
    for catalog, codes in (
        ("liveness_actions", ACTIONS),
        ("face_errors", FACE_ERRORS),
        ("verification_reasons", REASONS),
    ):
        for row in (r for r in seed[catalog] if r["code"] in codes):
            _insert_missing(catalog, row, ["code"])
    for catalog, _previous in PREVIOUS_MESSAGES:
        row = next(r for r in seed[catalog] if r["code"] == "LIVENESS_FAILED")
        _set_message(catalog, "LIVENESS_FAILED", row["message"])


def _challenges() -> None:
    op.add_column(
        "face_challenges", sa.Column("third_direction", sa.String(length=20), nullable=True), schema="biometrics"
    )
    op.add_column(
        "face_challenges", sa.Column("flash_colors", sa.String(length=80), nullable=True), schema="biometrics"
    )
    op.add_column(
        "face_challenges",
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        schema="biometrics",
    )
    op.create_foreign_key(
        op.f("fk_face_challenges_third_direction_liveness_actions"),
        "face_challenges",
        "liveness_actions",
        ["third_direction"],
        ["code"],
        source_schema="biometrics",
        referent_schema="catalog",
    )


def _policy() -> None:
    op.add_column(
        "verification_policy",
        sa.Column("liveness_timeout_seconds", sa.SmallInteger(), server_default=sa.text("60"), nullable=False),
        schema="tenancy",
    )
    op.add_column(
        "verification_policy",
        sa.Column("flash_liveness", sa.String(length=20), server_default="OBSERVE", nullable=False),
        schema="tenancy",
    )
    op.create_foreign_key(
        op.f("fk_verification_policy_flash_liveness_flash_modes"),
        "verification_policy",
        "flash_modes",
        ["flash_liveness"],
        ["code"],
        source_schema="tenancy",
        referent_schema="catalog",
    )
    op.drop_constraint(op.f(STEPS_CHECK), "verification_policy", schema="tenancy", type_="check")
    op.create_check_constraint(
        op.f(STEPS_CHECK), "verification_policy", "liveness_steps BETWEEN 1 AND 3", schema="tenancy"
    )
    op.create_check_constraint(
        op.f(TIMEOUT_CHECK), "verification_policy", "liveness_timeout_seconds BETWEEN 20 AND 180", schema="tenancy"
    )


def _metrics() -> None:
    op.create_table(
        "face_attempt_metrics",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=50), nullable=True),
        sa.Column("steps", sa.SmallInteger(), nullable=True),
        sa.Column("flash_mode", sa.String(length=20), nullable=True),
        sa.Column("response_seconds", sa.Float(), nullable=True),
        sa.Column("frontal_real_min", sa.Float(), nullable=True),
        sa.Column("frontal_real_mean", sa.Float(), nullable=True),
        sa.Column("step_real_min", sa.Float(), nullable=True),
        sa.Column("yaw_min", sa.Float(), nullable=True),
        sa.Column("pitch_min", sa.Float(), nullable=True),
        sa.Column("closer_min", sa.Float(), nullable=True),
        sa.Column("flash_score", sa.Float(), nullable=True),
        sa.Column("flash_magnitude", sa.Float(), nullable=True),
        sa.Column("flash_background", sa.Float(), nullable=True),
        sa.Column("quality_mean", sa.Float(), nullable=True),
        sa.Column("brightness_mean", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_face_attempt_metrics_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_face_attempt_metrics")),
        schema="ops",
    )
    op.create_index(
        "ix_face_attempt_metrics_company_created",
        "face_attempt_metrics",
        ["company_id", "created_at"],
        unique=False,
        schema="ops",
    )
    op.create_index(
        "ix_face_attempt_metrics_created", "face_attempt_metrics", ["created_at", "id"], unique=False, schema="ops"
    )
    op.create_table(
        "security_thresholds",
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_security_thresholds")),
        schema="ops",
    )


def _screen(seed: dict) -> None:
    _insert_missing("screens", next(r for r in seed["screens"] if r["code"] == SCREEN), ["code"])
    for grant in (g for g in seed["role_screens"] if g["screen_code"] == SCREEN):
        _insert_missing("role_screens", grant, ["role_code", "screen_code"])
    for link in (m for m in seed["menu_module_screens"] if m["screen_code"] == SCREEN):
        _insert_missing("menu_module_screens", link, ["screen_code"])


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    _catalogs(seed)
    _challenges()
    _policy()
    _metrics()
    _screen(seed)


def downgrade() -> None:
    for table_name in ("menu_module_screens", "role_screens"):
        links = sa.table(table_name, sa.column("screen_code"), schema="catalog")
        op.execute(sa.delete(links).where(links.c.screen_code == SCREEN))
    screens = sa.table("screens", sa.column("code"), schema="catalog")
    op.execute(sa.delete(screens).where(screens.c.code == SCREEN))
    op.drop_table("security_thresholds", schema="ops")
    op.drop_index("ix_face_attempt_metrics_created", table_name="face_attempt_metrics", schema="ops")
    op.drop_index("ix_face_attempt_metrics_company_created", table_name="face_attempt_metrics", schema="ops")
    op.drop_table("face_attempt_metrics", schema="ops")
    op.drop_constraint(op.f(TIMEOUT_CHECK), "verification_policy", schema="tenancy", type_="check")
    op.drop_constraint(op.f(STEPS_CHECK), "verification_policy", schema="tenancy", type_="check")
    # Las políticas con 3 movimientos vuelven al máximo anterior (2) antes de restaurar el CHECK.
    policy = sa.table("verification_policy", sa.column("liveness_steps"), schema="tenancy")
    op.execute(sa.update(policy).where(policy.c.liveness_steps > 2).values(liveness_steps=2))
    op.create_check_constraint(
        op.f(STEPS_CHECK), "verification_policy", "liveness_steps BETWEEN 1 AND 2", schema="tenancy"
    )
    op.drop_constraint(
        op.f("fk_verification_policy_flash_liveness_flash_modes"),
        "verification_policy",
        schema="tenancy",
        type_="foreignkey",
    )
    op.drop_column("verification_policy", "flash_liveness", schema="tenancy")
    op.drop_column("verification_policy", "liveness_timeout_seconds", schema="tenancy")
    # Los retos vigentes con un tercer movimiento ya no se podrían leer: se descartan (duran segundos).
    challenges = sa.table("face_challenges", sa.column("third_direction"), sa.column("direction"), schema="biometrics")
    op.execute(sa.delete(challenges).where(challenges.c.third_direction.is_not(None)))
    op.execute(sa.delete(challenges).where(challenges.c.direction.in_(ACTIONS)))
    op.drop_constraint(
        op.f("fk_face_challenges_third_direction_liveness_actions"),
        "face_challenges",
        schema="biometrics",
        type_="foreignkey",
    )
    for column in ("issued_at", "flash_colors", "third_direction"):
        op.drop_column("face_challenges", column, schema="biometrics")
    for catalog, previous in PREVIOUS_MESSAGES:
        _set_message(catalog, "LIVENESS_FAILED", previous)
    # La bitácora referencia los motivos nuevos: esos intentos quedan como prueba de vida no superada.
    logs = sa.table("verification_logs", sa.column("reason"), schema="attendance")
    op.execute(sa.update(logs).where(logs.c.reason.in_(REASONS)).values(reason="LIVENESS_FAILED"))
    for catalog, codes in (("verification_reasons", REASONS), ("face_errors", FACE_ERRORS)):
        table = sa.table(catalog, sa.column("code"), schema="catalog")
        op.execute(sa.delete(table).where(table.c.code.in_(codes)))
    actions = sa.table("liveness_actions", sa.column("code"), schema="catalog")
    op.execute(sa.delete(actions).where(actions.c.code.in_(ACTIONS)))
    op.drop_table("flash_modes", schema="catalog")
