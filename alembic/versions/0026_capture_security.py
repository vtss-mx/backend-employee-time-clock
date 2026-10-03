"""Candados contra engaños en la identificación facial, configurables por cada empresa

- catalog.antispoof_levels (nuevo): niveles de sensibilidad del anti-spoofing (Estándar, Alto,
  Máximo) con su umbral de probabilidad de rostro real y si basta una sola captura sospechosa.
- tenancy.verification_policy: un interruptor por candado (todos activos por defecto) —
  cámaras virtuales, imágenes que no son de la cámara (EXIF), foto fija, reenvío de capturas,
  continuidad de la toma, tiempo humano del reto, rostro duplicado y bloqueo temporal (con su
  número de intentos y minutos) —, el nivel de anti-spoofing y los giros de la prueba de vida (1 o 2).
- biometrics.face_challenges.second_direction: segundo giro aleatorio cuando se exigen dos.
- biometrics.capture_fingerprints (nueva): huella SHA-256 de los píxeles de cada captura recibida
  (no la imagen) para rechazar su reenvío; se conserva FACE_REPLAY_RETENTION_DAYS días.
- Catálogos: mensajes (face_errors) y motivos de la bitácora (verification_reasons) de los intentos
  sospechosos — también SPOOF_DETECTED, que antes no se registraba —, bloqueo, rostro ya registrado
  y la marca de revisión DUPLICATE_FACE.

En una base nueva 0020 ya cargó los registros nuevos de sus catálogos desde el seed; en una
existente se agregan aquí (ON CONFLICT DO NOTHING).

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-03 16:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
SECURITY = (
    "IMAGE_NOT_FROM_CAMERA",
    "STATIC_CAPTURE",
    "REPLAY_DETECTED",
    "CAPTURE_INCONSISTENT",
    "VIRTUAL_CAMERA",
    "CHALLENGE_TOO_FAST",
)
NEW_ROWS = {
    "face_errors": (*SECURITY, "FACE_ALREADY_REGISTERED", "FACE_LOCKED"),
    "verification_reasons": ("SPOOF_DETECTED", *SECURITY),
    "enrollment_flags": ("DUPLICATE_FACE",),
}
POLICY_SWITCHES = (
    "block_virtual_cameras",
    "reject_foreign_images",
    "detect_static_captures",
    "detect_replays",
    "check_capture_continuity",
    "enforce_human_timing",
    "detect_duplicate_faces",
    "lockout_enabled",
)
POLICY_CHECKS = (
    ("ck_verification_policy_liveness_steps", "liveness_steps BETWEEN 1 AND 2"),
    ("ck_verification_policy_lockout_max_failures", "lockout_max_failures BETWEEN 3 AND 20"),
    ("ck_verification_policy_lockout_minutes", "lockout_minutes BETWEEN 1 AND 1440"),
)


def _antispoof_levels(seed: dict) -> None:
    table = op.create_table(
        "antispoof_levels",
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("threshold", sa.Numeric(4, 3), nullable=False),
        sa.Column("any_frame", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_antispoof_levels")),
        schema="catalog",
    )
    op.bulk_insert(table, seed["antispoof_levels"])


def _policy_columns() -> None:
    policy = {"table_name": "verification_policy", "schema": "tenancy"}
    op.add_column(
        column=sa.Column("anti_spoofing_level", sa.String(length=30), server_default="STANDARD", nullable=False),
        **policy,
    )
    op.add_column(
        column=sa.Column("liveness_steps", sa.SmallInteger(), server_default=sa.text("2"), nullable=False), **policy
    )
    for switch in POLICY_SWITCHES:
        op.add_column(column=sa.Column(switch, sa.Boolean(), server_default=sa.true(), nullable=False), **policy)
    op.add_column(
        column=sa.Column("lockout_max_failures", sa.SmallInteger(), server_default=sa.text("5"), nullable=False),
        **policy,
    )
    op.add_column(
        column=sa.Column("lockout_minutes", sa.SmallInteger(), server_default=sa.text("15"), nullable=False), **policy
    )
    op.create_foreign_key(
        op.f("fk_verification_policy_anti_spoofing_level_antispoof_levels"),
        "verification_policy",
        "antispoof_levels",
        ["anti_spoofing_level"],
        ["code"],
        source_schema="tenancy",
        referent_schema="catalog",
    )
    op.create_index(
        op.f("ix_verification_policy_anti_spoofing_level"),
        "verification_policy",
        ["anti_spoofing_level"],
        schema="tenancy",
    )
    for name, condition in POLICY_CHECKS:
        op.create_check_constraint(op.f(name), "verification_policy", condition, schema="tenancy")


def _second_turn() -> None:
    op.add_column(
        "face_challenges", sa.Column("second_direction", sa.String(length=20), nullable=True), schema="biometrics"
    )
    op.create_foreign_key(
        op.f("fk_face_challenges_second_direction_liveness_actions"),
        "face_challenges",
        "liveness_actions",
        ["second_direction"],
        ["code"],
        source_schema="biometrics",
        referent_schema="catalog",
    )
    op.create_index(
        op.f("ix_face_challenges_second_direction"), "face_challenges", ["second_direction"], schema="biometrics"
    )


def _capture_fingerprints() -> None:
    op.create_table(
        "capture_fingerprints",
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_capture_fingerprints_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("digest", name=op.f("pk_capture_fingerprints")),
        schema="biometrics",
    )
    for column in ("company_id", "created_at"):
        op.create_index(
            op.f(f"ix_capture_fingerprints_{column}"), "capture_fingerprints", [column], schema="biometrics"
        )


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    _antispoof_levels(seed)
    _policy_columns()
    _second_turn()
    _capture_fingerprints()
    for catalog, codes in NEW_ROWS.items():
        for row in (r for r in seed[catalog] if r["code"] in codes):
            table = sa.table(catalog, *(sa.column(key) for key in row), schema="catalog")
            op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=["code"]))


def downgrade() -> None:
    for catalog, codes in reversed(NEW_ROWS.items()):
        table = sa.table(catalog, sa.column("code"), schema="catalog")
        op.execute(sa.delete(table).where(table.c.code.in_(codes)))
    op.drop_table("capture_fingerprints", schema="biometrics")
    op.drop_index(op.f("ix_face_challenges_second_direction"), table_name="face_challenges", schema="biometrics")
    op.drop_constraint(
        op.f("fk_face_challenges_second_direction_liveness_actions"),
        "face_challenges",
        schema="biometrics",
        type_="foreignkey",
    )
    op.drop_column("face_challenges", "second_direction", schema="biometrics")
    for name, _ in reversed(POLICY_CHECKS):
        op.drop_constraint(op.f(name), "verification_policy", schema="tenancy", type_="check")
    op.drop_index(
        op.f("ix_verification_policy_anti_spoofing_level"), table_name="verification_policy", schema="tenancy"
    )
    op.drop_constraint(
        op.f("fk_verification_policy_anti_spoofing_level_antispoof_levels"),
        "verification_policy",
        schema="tenancy",
        type_="foreignkey",
    )
    for column in ("lockout_minutes", "lockout_max_failures", *reversed(POLICY_SWITCHES), "liveness_steps"):
        op.drop_column("verification_policy", column, schema="tenancy")
    op.drop_column("verification_policy", "anti_spoofing_level", schema="tenancy")
    op.drop_table("antispoof_levels", schema="catalog")
