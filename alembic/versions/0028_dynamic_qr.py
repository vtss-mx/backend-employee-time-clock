"""QR dinámicos de un solo uso

El QR del empleado deja de ser una credencial fija (impresa o descargada): ahora lo genera el
empleado en su teléfono, vive unos segundos (lo decide cada empresa) y sirve UNA sola vez.

- tenancy.verification_policy.qr_lifetime_seconds: vigencia de cada QR (15 a 300 s, 30 por omisión).
- workforce.employee_qr_codes:
  - used_at / used_by_id / completed_at: cuándo y qué validador lo usó, y cuándo terminó el uso
    (con QR + rostro, al confirmar el rostro). Marcarlo usado es una sentencia atómica.
  - token_encrypted pasa a opcional: los QR dinámicos no guardan el token (solo su hash); queda
    únicamente en los QR fijos anteriores, que ya no se aceptan.
  - índice por vencimiento para depurar los vencidos.
- Catálogos: motivos ALREADY_USED (QR ya usado) y STATIC_QR (QR impreso, ya no válido); mensajes de
  REVOKED (reemplazado) y EXPIRED (vencido) y la descripción del método QR, para los QR dinámicos.

Los registros existentes no se modifican: los QR fijos simplemente dejan de aceptarse por su
formato ("TCQR1:") y el primer QR dinámico de cada empleado apaga el anterior.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-03 18:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
QR = {"table_name": "employee_qr_codes", "schema": "workforce"}
NEW_REASONS = ("ALREADY_USED", "STATIC_QR")
UPDATED = {"verification_reasons": ("REVOKED", "EXPIRED"), "verification_methods": ("QR",)}
#: Textos anteriores (para el downgrade).
PREVIOUS = {
    ("verification_reasons", "REVOKED"): {"name": "QR revocado", "description": None, "message": "QR no reconocido"},
    ("verification_reasons", "EXPIRED"): {
        "name": "QR expirado",
        "description": None,
        "message": "El QR ha expirado. Solicita uno nuevo a tu empresa",
    },
    ("verification_methods", "QR"): {"description": "Credencial impresa o en el teléfono del empleado"},
}


def _set_catalog_texts(catalog: str, code: str, values: dict[str, str | None]) -> None:
    table = sa.table(catalog, sa.column("code"), *(sa.column(key) for key in values), schema="catalog")
    op.execute(sa.update(table).where(table.c.code == code).values(**values))


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    op.add_column(
        "verification_policy",
        sa.Column("qr_lifetime_seconds", sa.SmallInteger(), server_default=sa.text("30"), nullable=False),
        schema="tenancy",
    )
    op.create_check_constraint(
        op.f("ck_verification_policy_qr_lifetime_seconds"),
        "verification_policy",
        "qr_lifetime_seconds BETWEEN 15 AND 300",
        schema="tenancy",
    )

    op.alter_column(column_name="token_encrypted", existing_type=sa.LargeBinary(), nullable=True, **QR)
    op.add_column(column=sa.Column("used_at", sa.DateTime(timezone=True), nullable=True), **QR)
    op.add_column(column=sa.Column("used_by_id", sa.Integer(), nullable=True), **QR)
    op.add_column(column=sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True), **QR)
    op.create_foreign_key(
        op.f("fk_employee_qr_codes_used_by_id_users"),
        "employee_qr_codes",
        "users",
        ["used_by_id"],
        ["id"],
        source_schema="workforce",
        referent_schema="auth",
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_employee_qr_codes_used_by_id",
        "employee_qr_codes",
        ["used_by_id"],
        schema="workforce",
        postgresql_where=sa.text("used_by_id IS NOT NULL"),
    )
    op.create_index(op.f("ix_employee_qr_codes_expires_at"), "employee_qr_codes", ["expires_at"], schema="workforce")

    for row in (r for r in seed["verification_reasons"] if r["code"] in NEW_REASONS):
        table = sa.table("verification_reasons", *(sa.column(key) for key in row), schema="catalog")
        op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=["code"]))
    for catalog, codes in UPDATED.items():
        for row in (r for r in seed[catalog] if r["code"] in codes):
            texts = {key: row[key] for key in PREVIOUS[(catalog, row["code"])]}
            _set_catalog_texts(catalog, row["code"], texts)


def downgrade() -> None:
    for (catalog, code), texts in PREVIOUS.items():
        _set_catalog_texts(catalog, code, texts)
    reasons = sa.table("verification_reasons", sa.column("code"), schema="catalog")
    op.execute(sa.delete(reasons).where(reasons.c.code.in_(NEW_REASONS)))

    op.drop_index(op.f("ix_employee_qr_codes_expires_at"), table_name="employee_qr_codes", schema="workforce")
    op.drop_index("ix_employee_qr_codes_used_by_id", table_name="employee_qr_codes", schema="workforce")
    op.drop_constraint(
        op.f("fk_employee_qr_codes_used_by_id_users"), "employee_qr_codes", schema="workforce", type_="foreignkey"
    )
    for column in ("completed_at", "used_by_id", "used_at"):
        op.drop_column(column_name=column, **QR)
    # Los QR dinámicos no tienen token cifrado: no pueden volver a la versión anterior.
    op.execute(sa.text("DELETE FROM workforce.employee_qr_codes WHERE token_encrypted IS NULL"))
    op.alter_column(column_name="token_encrypted", existing_type=sa.LargeBinary(), nullable=False, **QR)

    op.drop_constraint(
        op.f("ck_verification_policy_qr_lifetime_seconds"), "verification_policy", schema="tenancy", type_="check"
    )
    op.drop_column("verification_policy", "qr_lifetime_seconds", schema="tenancy")
