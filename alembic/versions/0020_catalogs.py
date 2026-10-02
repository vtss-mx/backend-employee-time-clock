"""Catálogos en la base de datos (esquema catalog), enlazados con llaves foráneas

catalog  roles, verification_methods, validator_modes, validator_mode_methods, face_statuses,
         enrollment_statuses, verification_reasons, accessories, liveness_actions, countries,
         enrollment_rejection_reasons, reverification_reasons, confidence_levels,
         session_revocation_reasons, face_errors, enrollment_flags

Las listas de valores dejan de vivir en el código: nombres, descripciones, mensajes, orden y si
están activas se leen de aquí. Los registros iniciales salen de alembic/seed/catalogs.json, la
misma fuente que usan las pruebas. Cada columna que guarda un código queda enlazada a su catálogo:

- auth.users.role (sustituye al CHECK fijo de roles) y auth.auth_sessions.revoked_reason
- workforce.validators.mode y workforce.employees.face_status
- attendance.verification_logs.method y .reason
- biometrics.face_enrollments.status y biometrics.face_challenges.direction
- tenancy.verification_policy.min_confidence → confidence_levels.value (pasa a numeric(7,5))
- biometrics.face_enrollment_flags (nueva): las marcas de revisión de cada registro facial, en
  lugar de la lista separada por comas face_enrollments.flagged_accessories.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-02 09:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG = "catalog"
SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
SEARCH_PATH = "auth, tenancy, workforce, biometrics, attendance, catalog, public"
PREVIOUS_SEARCH_PATH = "auth, tenancy, workforce, biometrics, attendance, public"
ROLES = "role IN ('ADMIN', 'COMPANY', 'EMPLOYEE', 'VALIDATOR')"

# (esquema, tabla, columna, catálogo, columna del catálogo) de cada columna que se enlaza.
REFERENCES = [
    ("auth", "users", "role", "roles", "code"),
    ("auth", "auth_sessions", "revoked_reason", "session_revocation_reasons", "code"),
    ("workforce", "validators", "mode", "validator_modes", "code"),
    ("workforce", "employees", "face_status", "face_statuses", "code"),
    ("attendance", "verification_logs", "method", "verification_methods", "code"),
    ("attendance", "verification_logs", "reason", "verification_reasons", "code"),
    ("biometrics", "face_enrollments", "status", "enrollment_statuses", "code"),
    ("biometrics", "face_challenges", "direction", "liveness_actions", "code"),
    ("tenancy", "verification_policy", "min_confidence", "confidence_levels", "value"),
]


def _extra_columns() -> dict[str, list[sa.Column]]:
    """Columnas propias de cada catálogo (además de code, name, description, sort_order y active)."""

    def tone() -> sa.Column:
        return sa.Column("tone", sa.String(length=20), nullable=False, server_default="muted")

    def message() -> sa.Column:
        return sa.Column("message", sa.String(length=200), nullable=False)

    return {
        "roles": [],
        "verification_methods": [],
        "validator_modes": [],
        "face_statuses": [tone(), sa.Column("employee_note", sa.String(length=120), nullable=False)],
        "enrollment_statuses": [tone()],
        "verification_reasons": [message()],
        "accessories": [sa.Column("phrase", sa.String(length=60), nullable=False)],
        "liveness_actions": [sa.Column("instruction", sa.String(length=120), nullable=False)],
        "countries": [
            sa.Column("dial_code", sa.String(length=6), nullable=False),
            sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.false()),
        ],
        "enrollment_rejection_reasons": [],
        "reverification_reasons": [],
        "confidence_levels": [
            sa.Column("value", sa.Numeric(7, 5), nullable=False),
            sa.Column("similarity", sa.Numeric(5, 3), nullable=False),
            sa.Column("false_accept_rate", sa.Numeric(6, 3), nullable=False),
            sa.Column("rejection_rate", sa.Numeric(5, 1), nullable=False),
            sa.UniqueConstraint("value", name=op.f("uq_confidence_levels_value")),
        ],
        "session_revocation_reasons": [message()],
        "face_errors": [message(), sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.true())],
        "enrollment_flags": [],
    }


def _create_catalog(name: str, extra: list[sa.Column]) -> sa.Table:
    return op.create_table(
        name,
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *extra,
        sa.PrimaryKeyConstraint("code", name=op.f(f"pk_{name}")),
        schema=CATALOG,
    )


def _create_mode_methods() -> sa.Table:
    return op.create_table(
        "validator_mode_methods",
        sa.Column("mode_code", sa.String(length=30), nullable=False),
        sa.Column("method_code", sa.String(length=30), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(
            ["mode_code"],
            [f"{CATALOG}.validator_modes.code"],
            name=op.f("fk_validator_mode_methods_mode_code_validator_modes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["method_code"],
            [f"{CATALOG}.verification_methods.code"],
            name=op.f("fk_validator_mode_methods_method_code_verification_methods"),
        ),
        sa.PrimaryKeyConstraint("mode_code", "method_code", name=op.f("pk_validator_mode_methods")),
        schema=CATALOG,
    )


def _set_database_search_path(value: str) -> None:
    op.execute(
        f"DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET search_path TO {value}', current_database()); END $$"
    )


def _fk_name(table: str, column: str, catalog: str) -> str:
    return f"fk_{table}_{column}_{catalog}"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}")
    _set_database_search_path(SEARCH_PATH)

    # 1. Catálogos y sus registros. Solo se cargan las columnas que existen en esta versión: el JSON
    #    describe el contenido vigente y migraciones posteriores pueden agregar columnas.
    tables = [_create_catalog(name, extra) for name, extra in _extra_columns().items()]
    tables.insert(3, _create_mode_methods())  # después de validator_modes y verification_methods
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    for table in tables:
        rows = [{key: value for key, value in row.items() if key in table.c} for row in seed.get(table.name, [])]
        if rows:
            op.bulk_insert(table, rows)

    # 2. La confianza mínima de cada empresa pasa a ser uno de los niveles del catálogo (el más cercano).
    op.alter_column("verification_policy", "min_confidence", schema="tenancy", server_default=None)
    op.alter_column(
        "verification_policy",
        "min_confidence",
        schema="tenancy",
        type_=sa.Numeric(7, 5),
        postgresql_using="round(min_confidence::numeric, 5)",
        server_default=sa.text("0.99999"),
    )
    op.execute(
        "UPDATE tenancy.verification_policy p SET min_confidence = (SELECT l.value FROM catalog.confidence_levels l "
        "ORDER BY abs(l.value - p.min_confidence) LIMIT 1)"
    )

    # 3. Cada código, enlazado a su catálogo. El rol ya no necesita el CHECK con la lista fija.
    op.drop_constraint(op.f("ck_users_role"), "users", schema="auth", type_="check")
    for schema, table, column, catalog, target in REFERENCES:
        op.create_foreign_key(
            op.f(_fk_name(table, column, catalog)),
            table,
            catalog,
            [column],
            [target],
            source_schema=schema,
            referent_schema=CATALOG,
        )

    # 4. Marcas de revisión del registro facial: de "MASK,SPOOF" a una fila por marca.
    op.create_table(
        "face_enrollment_flags",
        sa.Column("enrollment_id", sa.Integer(), nullable=False),
        sa.Column("flag_code", sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(
            ["enrollment_id"],
            ["biometrics.face_enrollments.id"],
            name=op.f("fk_face_enrollment_flags_enrollment_id_face_enrollments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["flag_code"],
            [f"{CATALOG}.enrollment_flags.code"],
            name=op.f("fk_face_enrollment_flags_flag_code_enrollment_flags"),
        ),
        sa.PrimaryKeyConstraint("enrollment_id", "flag_code", name=op.f("pk_face_enrollment_flags")),
        schema="biometrics",
    )
    op.execute(
        "INSERT INTO biometrics.face_enrollment_flags (enrollment_id, flag_code) "
        "SELECT DISTINCT e.id, trim(flag) FROM biometrics.face_enrollments e "
        "CROSS JOIN LATERAL unnest(string_to_array(e.flagged_accessories, ',')) AS flag WHERE trim(flag) <> ''"
    )
    op.drop_column("face_enrollments", "flagged_accessories", schema="biometrics")


def downgrade() -> None:
    op.add_column("face_enrollments", sa.Column("flagged_accessories", sa.String(length=60)), schema="biometrics")
    op.execute(
        "UPDATE biometrics.face_enrollments e SET flagged_accessories = (SELECT string_agg(f.flag_code, ',' "
        "ORDER BY f.flag_code) FROM biometrics.face_enrollment_flags f WHERE f.enrollment_id = e.id)"
    )
    op.drop_table("face_enrollment_flags", schema="biometrics")

    for schema, table, column, catalog, _ in reversed(REFERENCES):
        op.drop_constraint(op.f(_fk_name(table, column, catalog)), table, schema=schema, type_="foreignkey")
    op.create_check_constraint(op.f("ck_users_role"), "users", ROLES, schema="auth")

    op.alter_column("verification_policy", "min_confidence", schema="tenancy", server_default=None)
    op.alter_column(
        "verification_policy",
        "min_confidence",
        schema="tenancy",
        type_=sa.Float(),
        postgresql_using="min_confidence::double precision",
        server_default=sa.text("0.99999"),
    )

    op.drop_table("validator_mode_methods", schema=CATALOG)
    for name in reversed(list(_extra_columns())):
        op.drop_table(name, schema=CATALOG)
    _set_database_search_path(PREVIOUS_SEARCH_PATH)
    op.execute(f"DROP SCHEMA IF EXISTS {CATALOG}")
