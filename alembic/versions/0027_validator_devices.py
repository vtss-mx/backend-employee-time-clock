"""Dispositivos de los validadores: cada uno se enrola y la empresa lo autoriza

- catalog.device_statuses (nuevo): PENDING (por autorizar), APPROVED, REJECTED, REVOKED.
- workforce.validator_devices (nueva): llave pública ECDSA P-256 de cada dispositivo (tableta o
  teléfono) en el que inició sesión un validador, su nombre, estado y quién la revisó. El dispositivo
  firma un reto del servidor en cada inicio de sesión con su llave no exportable: la contraseña
  sola no basta y la sesión no se puede trasladar a otro equipo.
- tenancy.verification_policy.validator_device_approval: la empresa decide si exige autorizar cada
  dispositivo (activo por defecto).
- catalog.session_revocation_reasons: DEVICE_REVOKED (retirar la autorización cierra sus sesiones).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-03 20:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    statuses = op.create_table(
        "device_statuses",
        sa.Column("tone", sa.String(length=20), server_default="muted", nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_device_statuses")),
        schema="catalog",
    )
    op.bulk_insert(statuses, seed["device_statuses"])

    op.create_table(
        "validator_devices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("validator_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("public_key", sa.String(length=300), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("user_agent", sa.String(length=400), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ip", sa.String(length=64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["validator_id"],
            ["workforce.validators.id"],
            name=op.f("fk_validator_devices_validator_id_validators"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_validator_devices_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["status"], ["catalog.device_statuses.code"], name=op.f("fk_validator_devices_status_device_statuses")
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"],
            ["auth.users.id"],
            name=op.f("fk_validator_devices_reviewed_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validator_devices")),
        sa.UniqueConstraint("validator_id", "key_hash", name=op.f("uq_validator_devices_validator_id")),
        schema="workforce",
    )
    op.create_index(
        op.f("ix_validator_devices_validator_id"), "validator_devices", ["validator_id"], schema="workforce"
    )
    op.create_index(
        op.f("ix_validator_devices_company_status"), "validator_devices", ["company_id", "status"], schema="workforce"
    )
    op.create_index(op.f("ix_validator_devices_status"), "validator_devices", ["status"], schema="workforce")
    op.create_index(
        op.f("ix_validator_devices_reviewed_by_id"),
        "validator_devices",
        ["reviewed_by_id"],
        schema="workforce",
        postgresql_where=sa.text("reviewed_by_id IS NOT NULL"),
    )

    op.add_column(
        "verification_policy",
        sa.Column("validator_device_approval", sa.Boolean(), server_default=sa.true(), nullable=False),
        schema="tenancy",
    )

    # En una base nueva 0020 ya cargó el motivo desde el seed; en una existente se agrega aquí.
    row = next(r for r in seed["session_revocation_reasons"] if r["code"] == "DEVICE_REVOKED")
    reasons = sa.table("session_revocation_reasons", *(sa.column(key) for key in row), schema="catalog")
    op.execute(postgresql.insert(reasons).values(**row).on_conflict_do_nothing(index_elements=["code"]))


def downgrade() -> None:
    reasons = sa.table("session_revocation_reasons", sa.column("code"), schema="catalog")
    op.execute(sa.delete(reasons).where(reasons.c.code == "DEVICE_REVOKED"))
    op.drop_column("verification_policy", "validator_device_approval", schema="tenancy")
    op.drop_table("validator_devices", schema="workforce")
    op.drop_table("device_statuses", schema="catalog")
