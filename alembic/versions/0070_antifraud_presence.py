"""Antifraude de identidad, fase 2b: firma por petición y ubicación en cada identificación de los validadores, y código
de sitio con su kiosco

La segunda mitad de la fase 2 de `docs/rd/antifraude-identidad.md` (§2.6, §5.2 y §7.1; riesgos R1 y R7), con la decisión
D9 del dueño del producto (código de sitio opcional por sitio). Todo nace en «Solo medir»: mide desde el primer día y no
rechaza a nadie hasta calibrarlo.

- `tenancy.verification_policy`: `validator_signing`, `validator_location` y `site_codes` (modos de
  `catalog.signal_modes`, `OBSERVE`; una fila por empresa: el valor constante no reescribe la tabla).
- `auth.auth_sessions.device_key_hash`: la llave del dispositivo a la que queda ligada la sesión de un validador (nula
  en las sesiones abiertas antes: se ligan a la primera llave que firma).
- `workforce.work_sites`: `presence_code` (apagado) y `presence_secret` (cifrado con DATA_ENCRYPTION_KEY, texto Fernet:
  ninguna columna binaria nueva).
- `attendance.attendance_events.presence_window` (particionada): el periodo del código de sitio con que se confirmó un
  registro (nunca el código). Nula y sin valor por omisión: pasa a todas sus particiones al instante.
- `workforce.site_kiosks` (nueva, de empresa, con borrado lógico): los kioscos de cada sitio. Pequeña (unas cuantas
  tabletas por sitio): no se particiona. Seguridad por fila como toda tabla de empresa, FK compuesta hacia su sitio y
  `fillfactor` 90 (cada tanto anota cuándo pidió su código).
- Catálogos: 8 señales del motor de riesgo (`risk_signals`, todas en «Solo medir»; `VALIDATOR_SIGNATURE_INVALID` es una
  regla dura que también nace midiendo) y el motivo de revisión `PRESENCE`, con su traducción a en-US.

No se agrega un catálogo nuevo para los modos de los tres interruptores: la migración 0064 carga las traducciones de
TODOS los catálogos del archivo vigente, así que una tabla de catálogo nueva rompería una base nueva en esa migración;
los modos son los de una señal (apagada, solo medir u obligatoria) y se reutiliza `signal_modes`.

`downgrade` quita lo agregado (tabla, columnas, señales y motivo con su línea base y sus traducciones).

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-06 09:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
CATALOG = "catalog"
TENANCY = "tenancy"
WORKFORCE = "workforce"
ATTENDANCE = "attendance"
AUTH = "auth"
LOCALE = "en-US"
TABLE = "site_kiosks"
#: La seguridad por fila de la tabla nueva (la misma SQL que `row_security.policy_ddl`), en sentencias constantes.
ROW_SECURITY = (
    "ALTER TABLE workforce.site_kiosks ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE workforce.site_kiosks FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON workforce.site_kiosks "
    "USING (company_id = NULLIF(current_setting('app.company_id', true), '')::integer) "
    "WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), '')::integer)",
    "COMMENT ON COLUMN workforce.site_kiosks.company_id IS 'Empresa dueña de la fila. Seguridad por fila (política "
    "tenant_isolation): solo se ve y se escribe en una transacción con app.company_id igual (o con el rol de la "
    "plataforma).'",
)
NEW_REVIEW_REASONS = ("PRESENCE",)
NEW_SIGNALS = (
    "VALIDATOR_UNSIGNED",
    "VALIDATOR_SIGNATURE_INVALID",
    "VALIDATOR_KEY_MISMATCH",
    "VALIDATOR_LOCATION_MISSING",
    "VALIDATOR_LOCATION_INACCURATE",
    "VALIDATOR_OUT_OF_ZONE",
    "SITE_CODE_MISSING",
    "SITE_CODE_INVALID",
)
POLICY_COLUMNS = ("validator_signing", "validator_location", "site_codes")
#: Lo que `downgrade` borra de los catálogos (los códigos van como parámetros, nunca dentro de la sentencia).
DOWNGRADE_DELETES = (
    ("DELETE FROM catalog.translations WHERE catalog = 'risk_signals' AND code IN :codes", NEW_SIGNALS),
    ("DELETE FROM catalog.translations WHERE catalog = 'review_reasons' AND code IN :codes", NEW_REVIEW_REASONS),
    ("DELETE FROM ops.risk_signal_stats WHERE signal IN :codes", NEW_SIGNALS),
    ("DELETE FROM catalog.risk_signals WHERE code IN :codes", NEW_SIGNALS),
    ("DELETE FROM catalog.review_reasons WHERE code IN :codes", NEW_REVIEW_REASONS),
)
_TRANSLATIONS = sa.table(
    "translations",
    sa.column("catalog"),
    sa.column("code"),
    sa.column("locale"),
    sa.column("field"),
    sa.column("text"),
    schema=CATALOG,
)


def _seed(name: str) -> dict:
    return json.loads((SEED_DIR / name).read_text(encoding="utf-8"))


def _catalog(seed: dict, english: dict) -> None:
    """El motivo y las señales nuevos (idempotente: una base nueva ya los cargó con el seed vigente) y sus textos en
    inglés (inserción o actualización)."""
    for catalog, codes in (("review_reasons", NEW_REVIEW_REASONS), ("risk_signals", NEW_SIGNALS)):
        items = [row for row in seed[catalog] if row["code"] in codes]
        table = sa.table(catalog, *(sa.column(key) for key in items[0]), schema=CATALOG)
        op.execute(postgresql.insert(table).values(items).on_conflict_do_nothing(index_elements=["code"]))
    rows = [
        {"catalog": catalog, "code": code, "locale": LOCALE, "field": field, "text": text}
        for catalog, codes in (("review_reasons", NEW_REVIEW_REASONS), ("risk_signals", NEW_SIGNALS))
        for code in codes
        for field, text in english[catalog][code].items()
    ]
    statement = postgresql.insert(_TRANSLATIONS).values(rows)
    op.execute(
        statement.on_conflict_do_update(
            index_elements=["catalog", "code", "locale", "field"], set_={"text": statement.excluded.text}
        )
    )


def _policy() -> None:
    for name in POLICY_COLUMNS:
        op.add_column(
            "verification_policy",
            sa.Column(name, sa.String(length=20), server_default="OBSERVE", nullable=False),
            schema=TENANCY,
        )
        op.create_foreign_key(
            op.f(f"fk_verification_policy_{name}_signal_modes"),
            "verification_policy",
            "signal_modes",
            [name],
            ["code"],
            source_schema=TENANCY,
            referent_schema=CATALOG,
        )


def _kiosks() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("pairing_hash", sa.String(length=64), nullable=True),
        sa.Column("pairing_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("key_hash", sa.String(length=64), nullable=True),
        sa.Column("public_key", sa.String(length=300), nullable=True),
        sa.Column("device_name", sa.String(length=120), nullable=True),
        sa.Column("paired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_site_kiosks_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "company_id"],
            [f"{WORKFORCE}.work_sites.id", f"{WORKFORCE}.work_sites.company_id"],
            name="fk_site_kiosks_site_company",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site_kiosks")),
        schema=WORKFORCE,
        postgresql_with={"fillfactor": 90},
    )
    op.create_index(
        "ix_site_kiosks_site",
        TABLE,
        ["company_id", "site_id", "id"],
        schema=WORKFORCE,
        postgresql_include=["deleted_at"],
    )
    op.create_index(
        "uq_site_kiosks_pairing",
        TABLE,
        ["pairing_hash"],
        unique=True,
        schema=WORKFORCE,
        postgresql_where=sa.text("pairing_hash IS NOT NULL"),
    )
    op.create_index(
        "ix_site_kiosks_deleted",
        TABLE,
        ["company_id", "site_id", "deleted_at", "id"],
        schema=WORKFORCE,
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )
    for statement in ROW_SECURITY:
        op.execute(statement)


def upgrade() -> None:
    _catalog(_seed("catalogs.json"), _seed(f"catalogs.{LOCALE}.json"))
    _policy()
    op.add_column("auth_sessions", sa.Column("device_key_hash", sa.String(length=64), nullable=True), schema=AUTH)
    op.add_column(
        "work_sites",
        sa.Column("presence_code", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        schema=WORKFORCE,
    )
    op.add_column("work_sites", sa.Column("presence_secret", sa.String(length=255), nullable=True), schema=WORKFORCE)
    # Tabla particionada: la columna nueva (nula, sin valor por omisión) pasa a todas sus particiones al instante.
    op.add_column("attendance_events", sa.Column("presence_window", sa.Integer(), nullable=True), schema=ATTENDANCE)
    _kiosks()
    op.get_bind().exec_driver_sql((SQL_DIR / "0070_comments.sql").read_text(encoding="utf-8").replace("%", "%%"))


def downgrade() -> None:
    op.drop_table(TABLE, schema=WORKFORCE)
    op.drop_column("attendance_events", "presence_window", schema=ATTENDANCE)
    op.drop_column("work_sites", "presence_secret", schema=WORKFORCE)
    op.drop_column("work_sites", "presence_code", schema=WORKFORCE)
    op.drop_column("auth_sessions", "device_key_hash", schema=AUTH)
    for name in reversed(POLICY_COLUMNS):
        op.drop_column("verification_policy", name, schema=TENANCY)
    bind = op.get_bind()
    for statement, codes in DOWNGRADE_DELETES:
        bind.execute(sa.text(statement).bindparams(sa.bindparam("codes", expanding=True)), {"codes": list(codes)})
