"""Validadores por empresa: el ADMIN decide cuántos puede tener cada empresa (0 = sin el módulo) y cada validador
activo cuenta como un empleado en el cobro

Decisiones del dueño del producto: «como admin debo tener la capacidad de validar cuántos validadores tendrá una
empresa; si la empresa tiene 0, el módulo no debe estar habilitado» y «1 validador cuenta como un empleado para que lo
contemples en los costos».

- `tenancy.companies.max_validators` (NOT NULL, 0 por omisión, CHECK >= 0): validadores ACTIVOS que puede tener. Las
  empresas nuevas empiezan en 0 (el módulo es opcional y cuesta). Las que ya existen reciben cuántas cuentas de
  validador tienen (activas o no): nada se rompe y cada una puede seguir activando las suyas. El valor constante por
  omisión no reescribe la tabla; el UPDATE solo toca las empresas con validadores (pocas filas).
- `workforce.validator_status_events` (nueva, de empresa): altas, bajas y reactivaciones de cada validador, igual que
  `employee_status_events` (la plantilla diaria sale SOLO de historiales de estado). Sin FK al validador a propósito
  (eliminarlo no borra los días que estuvo activo). Seguridad por fila como toda tabla de empresa. Su historial empieza
  con un alta, en el instante de esta migración, de cada validador ACTIVO: el cobro de los validadores empieza con este
  despliegue (los días ya cerrados se quedan como se cobraron, en 0).
- `billing.headcount_days.active_validators` (NOT NULL, 0 por omisión, CHECK >= 0): los validadores activos de cada
  día, aparte de los empleados para mostrar el desglose. El cobro por empleado activo suma los dos.
- Catálogo: la descripción del modo «Por empleado activo» dice que cada validador cuenta como un empleado, en los dos
  idiomas.

Corre con el dueño de la base (en docker compose es superusuario: la seguridad por fila no le aplica al leer los
validadores; en una base administrada, el usuario de migraciones necesita `BYPASSRLS`).

`downgrade` deja todo como estaba: quita las columnas, sus CHECK y la tabla, y regresa los textos del catálogo.

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-05 21:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
CATALOG = "catalog"
TENANCY = "tenancy"
WORKFORCE = "workforce"
BILLING = "billing"
LOCALE = "en-US"
TABLE = "validator_status_events"
EXPRESSION = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
COMPANY_COMMENT = (
    "Empresa dueña de la fila. Seguridad por fila (política tenant_isolation): solo se ve y se escribe en una "
    "transacción con app.company_id igual (o con el rol de la plataforma)."
)
#: Textos que cambian: (catálogo, código, columna) → (español anterior, inglés anterior). El nuevo sale del seed.
PREVIOUS_TEXTS = {
    ("pricing_modes", "PER_USER", "description"): (
        "Cada empleado activo, día por día: quien entra o sale a mitad del periodo paga solo sus días.",
        "Each active employee, day by day: anyone who joins or leaves mid-period pays only for their days.",
    ),
}


def _seed(name: str) -> dict:
    return json.loads((SEED_DIR / name).read_text(encoding="utf-8"))


def _set_texts(texts: dict[tuple[str, str, str], tuple[str, str]]) -> None:
    """Cada texto en español (columna del catálogo) y en inglés (`catalog.translations`)."""
    bind = op.get_bind()
    for (catalog, code, column), (spanish, english) in texts.items():
        bind.execute(
            sa.text(f"UPDATE {CATALOG}.{catalog} SET {column} = :text WHERE code = :code"),
            {"text": spanish, "code": code},
        )
        bind.execute(
            sa.text(
                f"UPDATE {CATALOG}.translations SET text = :text "
                "WHERE catalog = :catalog AND code = :code AND locale = :locale AND field = :field"
            ),
            {"text": english, "catalog": catalog, "code": code, "locale": LOCALE, "field": column},
        )


def _current_texts() -> dict[tuple[str, str, str], tuple[str, str]]:
    seed, english = _seed("catalogs.json"), _seed(f"catalogs.{LOCALE}.json")
    return {
        (catalog, code, column): (
            next(row[column] for row in seed[catalog] if row["code"] == code),
            english[catalog][code][column],
        )
        for catalog, code, column in PREVIOUS_TEXTS
    }


def _limits() -> None:
    op.add_column(
        "companies",
        sa.Column("max_validators", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema=TENANCY,
    )
    op.create_check_constraint(op.f("ck_companies_max_validators"), "companies", "max_validators >= 0", schema=TENANCY)
    # Las empresas que ya tienen validadores conservan todos los suyos (activos o no).
    op.execute(
        f"UPDATE {TENANCY}.companies AS c SET max_validators = v.accounts "
        "FROM (SELECT company_id, count(*) AS accounts FROM auth.users WHERE role = 'VALIDATOR' "
        "GROUP BY company_id) AS v WHERE v.company_id = c.id"
    )


def _status_events() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("validator_id", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            [f"{TENANCY}.companies.id"],
            name=op.f("fk_validator_status_events_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validator_status_events")),
        schema=WORKFORCE,
    )
    # El cobro de los validadores empieza ahora: un alta de cada validador activo (antes de la seguridad por fila).
    op.execute(
        f"INSERT INTO {WORKFORCE}.{TABLE} (company_id, validator_id, active, occurred_at) "
        f"SELECT v.company_id, v.id, true, now() FROM {WORKFORCE}.validators AS v "
        "JOIN auth.users AS u ON u.id = v.user_id WHERE u.active"
    )
    op.create_index(
        "ix_validator_status_events_company_validator",
        TABLE,
        ["company_id", "validator_id", "occurred_at", "id"],
        unique=False,
        schema=WORKFORCE,
        postgresql_include=["active"],
    )
    op.create_index("ix_validator_status_events_occurred_at", TABLE, ["occurred_at"], unique=False, schema=WORKFORCE)
    qualified = f"{WORKFORCE}.{TABLE}"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {qualified} USING ({EXPRESSION}) WITH CHECK ({EXPRESSION})")
    op.execute(f"COMMENT ON COLUMN {qualified}.company_id IS '{COMPANY_COMMENT}'")


def _headcount() -> None:
    op.add_column(
        "headcount_days",
        sa.Column("active_validators", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema=BILLING,
    )
    op.create_check_constraint(
        op.f("ck_headcount_days_active_validators"), "headcount_days", "active_validators >= 0", schema=BILLING
    )


def upgrade() -> None:
    _limits()
    _status_events()
    _headcount()
    _set_texts(_current_texts())
    op.get_bind().exec_driver_sql((SQL_DIR / "0067_comments.sql").read_text(encoding="utf-8").replace("%", "%%"))


def downgrade() -> None:
    _set_texts(PREVIOUS_TEXTS)
    op.drop_constraint(op.f("ck_headcount_days_active_validators"), "headcount_days", schema=BILLING, type_="check")
    op.drop_column("headcount_days", "active_validators", schema=BILLING)
    op.drop_table(TABLE, schema=WORKFORCE)
    op.drop_constraint(op.f("ck_companies_max_validators"), "companies", schema=TENANCY, type_="check")
    op.drop_column("companies", "max_validators", schema=TENANCY)
