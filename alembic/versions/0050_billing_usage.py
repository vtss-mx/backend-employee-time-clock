"""Cobranza de las empresas y monitoreo de su consumo

El dueño del producto pidió integrar al alta de empresas todo lo del cobro (las reglas puras ya
existían en `app/services/billing_rules.py`) y poder monitorear en todo momento costos, pagos y el
consumo de datos de cada empresa. Decisiones (detalle en el README, "Cobranza" y "Consumo"):

- Se cobra por empleado ACTIVO prorrateado por día (validadores y administradores no cuentan) o un
  monto fijo por empresa; precios sin IVA y el IVA a la tasa de cada empresa (16 % por omisión); corte
  el último día del mes y un cargo cada N meses; demo de N días; descuento en todos los cargos, en los
  primeros N o cada N. Pagos registrados a mano por el ADMIN (sin pasarela), aplicados al cargo abierto
  más antiguo (lo que sobra queda a favor). Suspensión automática después de los días de gracia y
  manual; suspender cierra al momento todas las sesiones de la empresa.

Estructura:

- Esquema nuevo `billing`: `plans` (uno por empresa; sin plan no se cobra), `headcount_days`
  (empleados activos de cada empresa por día: la base del prorrateo), `charges` + `charge_lines`
  (cargo por corte con su copia del plan y sus líneas por mes), `payments` + `payment_receipts` (el
  comprobante, aparte para que ningún listado lo lea) + `payment_allocations` (qué pago cubrió qué
  cargo). Cargos y pagos con FK `RESTRICT` a la empresa: el historial financiero nunca se borra.
- `workforce.employee_status_events`: altas, bajas y reactivaciones (sin FK al empleado: borrarlo no
  borra los días que estuvo activo). Se llena con los empleados que ya existen: un alta en su
  `created_at` y, si hoy está inactivo, una baja en su `updated_at` (la mejor aproximación disponible;
  solo importa si un plan se fecha antes de esta migración).
- `tenancy.companies`: `suspended_at`, `suspension_reason`, `suspension_note`, `suspended_by`, junto a
  `active`: la autenticación de cada petición ya carga la empresa, así que revisar la suspensión no
  cuesta ninguna consulta.
- Esquema `ops`: `usage_daily`, `usage_routes`, `usage_users` (consumo por empresa/día, por ruta y por
  cuenta; ids sin FK a propósito: 0 = sin empresa / anónimo), `storage_snapshots` (foto diaria del
  almacenamiento por grupo) y `daily_tasks` (qué tareas diarias del mantenimiento ya se hicieron).
- Catálogos: `pricing_modes`, `price_periods`, `discount_types`, `discount_recurrences`,
  `billing_statuses`, `suspension_reasons`, `charge_statuses`, `payment_statuses`, `payment_methods`,
  `storage_categories`, y el motivo de cierre de sesión `COMPANY_SUSPENDED`.
- Menú: módulo "Negocio" (después de Plataforma; los demás bajan un lugar) con las pantallas
  `ADMIN_BILLING` ("Cobranza") y `ADMIN_USAGE` ("Consumo") del ADMIN.

Índices (cada uno con la consulta que sirve en su modelo): planes por próximo corte; cargos abiertos
por vencimiento (parcial), por emisión y por corte; pagos por empresa y fecha, por fecha y los que
tienen saldo sin aplicar (parcial); aplicaciones por cargo; eventos por empresa y empleado en orden
(INCLUDE active) y por instante; consumo y fotos por día (resumen y depuración); empresas suspendidas
(parcial). Todas las tablas son nuevas (o `companies`, chica): se crean sin `CONCURRENTLY`.

No cambia datos de las empresas: ninguna recibe un plan (el ADMIN se lo asigna) ni se suspende.

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-05 10:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
BILLING = "billing"
CATALOGS = (
    "pricing_modes",
    "price_periods",
    "discount_types",
    "discount_recurrences",
    "billing_statuses",
    "suspension_reasons",
    "charge_statuses",
    "payment_statuses",
    "payment_methods",
    "storage_categories",
)
#: Catálogos con color (`tone`).
TONED = {"billing_statuses", "charge_statuses", "payment_statuses"}
SCREENS = ("ADMIN_BILLING", "ADMIN_USAGE")
MODULE = "BUSINESS"
REASON = "COMPANY_SUSPENDED"
#: El módulo nuevo va en el lugar 2: los que estaban de ahí en adelante bajan uno.
MODULE_POSITION = 2
COUNTERS = ("requests", "bytes_in", "bytes_out", "duration_ms", "server_errors", "client_errors")


def _money() -> sa.Numeric:
    return sa.Numeric(14, 2)


def _catalog_fk(table: str, column: str, catalog: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"catalog.{catalog}.code"], name=op.f(f"fk_{table}_{column}_{catalog}"))


def _company_fk(table: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["company_id"], ["tenancy.companies.id"], name=op.f(f"fk_{table}_company_id_companies"), ondelete=ondelete
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _insert_missing(table_name: str, row: dict, keys: list[str]) -> None:
    """Inserta la fila del seed si no está (una base nueva ya la tiene: 0020/0021/0043 copian el seed)."""
    table = sa.table(table_name, *(sa.column(key) for key in row), schema="catalog")
    op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=keys))


# ---------------------------------------------------------------- catálogos y menú


def _catalogs(seed: dict) -> None:
    for name in CATALOGS:
        extra = [sa.Column("tone", sa.String(length=20), server_default="muted", nullable=False)] if name in TONED else []
        table = op.create_table(
            name,
            *extra,
            sa.Column("code", sa.String(length=30), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("description", sa.String(length=300), nullable=True),
            sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.PrimaryKeyConstraint("code", name=op.f(f"pk_{name}")),
            schema="catalog",
        )
        op.bulk_insert(table, seed[name])
    _insert_missing("session_revocation_reasons", next(r for r in seed["session_revocation_reasons"] if r["code"] == REASON), ["code"])


def _menu(seed: dict) -> None:
    _insert_missing("menu_modules", next(r for r in seed["menu_modules"] if r["code"] == MODULE), ["code"])
    modules = sa.table("menu_modules", sa.column("code"), sa.column("sort_order"), schema="catalog")
    for row in seed["menu_modules"]:  # el orden del seed: Negocio después de Plataforma
        op.execute(sa.update(modules).where(modules.c.code == row["code"]).values(sort_order=row["sort_order"]))
    for row in (r for r in seed["screens"] if r["code"] in SCREENS):
        _insert_missing("screens", row, ["code"])
    for grant in (g for g in seed["role_screens"] if g["screen_code"] in SCREENS):
        _insert_missing("role_screens", grant, ["role_code", "screen_code"])
    for link in (m for m in seed["menu_module_screens"] if m["screen_code"] in SCREENS):
        _insert_missing("menu_module_screens", link, ["screen_code"])


# ---------------------------------------------------------------- empresas y empleados


def _companies() -> None:
    with op.batch_alter_table("companies", schema="tenancy") as batch_op:
        batch_op.add_column(sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("suspension_reason", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("suspension_note", sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column("suspended_by", sa.String(length=255), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_companies_suspension_reason_suspension_reasons"),
            "suspension_reasons",
            ["suspension_reason"],
            ["code"],
            referent_schema="catalog",
        )
        batch_op.create_check_constraint(
            op.f("ck_companies_suspension"), "(suspended_at IS NULL) = (suspension_reason IS NULL)"
        )
        batch_op.create_index(
            "ix_companies_suspended_at",
            ["suspended_at"],
            unique=False,
            postgresql_where=sa.text("suspended_at IS NOT NULL"),
        )


def _status_events() -> None:
    op.create_table(
        "employee_status_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _company_fk("employee_status_events", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_status_events")),
        schema="workforce",
    )
    # El historial de los empleados que ya existen: un alta al crearse y, si hoy está inactivo, una baja
    # en su última actualización (la mejor aproximación disponible).
    op.execute(
        "INSERT INTO workforce.employee_status_events (company_id, employee_id, active, occurred_at) "
        "SELECT company_id, id, true, created_at FROM workforce.employees"
    )
    op.execute(
        "INSERT INTO workforce.employee_status_events (company_id, employee_id, active, occurred_at) "
        "SELECT company_id, id, false, GREATEST(updated_at, created_at) FROM workforce.employees WHERE NOT active"
    )
    op.create_index(
        "ix_employee_status_events_company_employee",
        "employee_status_events",
        ["company_id", "employee_id", "occurred_at", "id"],
        unique=False,
        schema="workforce",
        postgresql_include=["active"],
    )
    op.create_index(
        "ix_employee_status_events_occurred_at", "employee_status_events", ["occurred_at"], unique=False, schema="workforce"
    )


# ---------------------------------------------------------------- cobranza


def _plans() -> None:
    op.create_table(
        "plans",
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("pricing_mode", sa.String(length=20), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("price_period", sa.String(length=20), nullable=False),
        sa.Column("interval_months", sa.SmallInteger(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("trial_days", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("trial_ends_on", sa.Date(), nullable=True),
        sa.Column("discount_type", sa.String(length=20), nullable=True),
        sa.Column("discount_value", sa.Numeric(12, 2), nullable=True),
        sa.Column("discount_recurrence", sa.String(length=20), nullable=True),
        sa.Column("discount_periods", sa.SmallInteger(), nullable=True),
        sa.Column("tax_rate", sa.Numeric(5, 2), server_default=sa.text("16"), nullable=False),
        sa.Column("grace_days", sa.SmallInteger(), server_default=sa.text("10"), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="MXN", nullable=False),
        sa.Column("next_cut_on", sa.Date(), nullable=False),
        sa.Column("grace_until", sa.Date(), nullable=True),
        sa.Column("forecast_total", _money(), nullable=True),
        sa.Column("forecast_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("unit_price >= 0", name=op.f("ck_plans_unit_price")),
        sa.CheckConstraint("interval_months BETWEEN 1 AND 12", name=op.f("ck_plans_interval_months")),
        sa.CheckConstraint("trial_days BETWEEN 0 AND 365", name=op.f("ck_plans_trial_days")),
        sa.CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name=op.f("ck_plans_tax_rate")),
        sa.CheckConstraint("grace_days BETWEEN 0 AND 90", name=op.f("ck_plans_grace_days")),
        sa.CheckConstraint(
            "(discount_type IS NULL) = (discount_value IS NULL)"
            " AND (discount_type IS NULL) = (discount_recurrence IS NULL)",
            name=op.f("ck_plans_discount_complete"),
        ),
        sa.CheckConstraint("discount_value IS NULL OR discount_value > 0", name=op.f("ck_plans_discount_value")),
        sa.CheckConstraint(
            "discount_periods IS NULL OR discount_periods BETWEEN 1 AND 120", name=op.f("ck_plans_discount_periods")
        ),
        _company_fk("plans", "CASCADE"),
        _catalog_fk("plans", "pricing_mode", "pricing_modes"),
        _catalog_fk("plans", "price_period", "price_periods"),
        _catalog_fk("plans", "discount_type", "discount_types"),
        _catalog_fk("plans", "discount_recurrence", "discount_recurrences"),
        sa.PrimaryKeyConstraint("company_id", name=op.f("pk_plans")),
        schema=BILLING,
    )
    op.create_index("ix_plans_next_cut_on", "plans", ["next_cut_on"], unique=False, schema=BILLING)
    op.create_table(
        "headcount_days",
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("active_employees", sa.Integer(), nullable=False),
        sa.CheckConstraint("active_employees >= 0", name=op.f("ck_headcount_days_active_employees")),
        _company_fk("headcount_days", "CASCADE"),
        sa.PrimaryKeyConstraint("company_id", "day", name=op.f("pk_headcount_days")),
        schema=BILLING,
    )


def _charges() -> None:
    op.create_table(
        "charges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("cut_on", sa.Date(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("billable_days", sa.SmallInteger(), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("pricing_mode", sa.String(length=20), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("price_period", sa.String(length=20), nullable=False),
        sa.Column("tax_rate", sa.Numeric(5, 2), nullable=False),
        sa.Column("subtotal", _money(), nullable=False),
        sa.Column("discount", _money(), nullable=False),
        sa.Column("tax", _money(), nullable=False),
        sa.Column("total", _money(), nullable=False),
        sa.Column("paid", _money(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="OPEN", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by", sa.String(length=255), nullable=True),
        sa.Column("void_reason", sa.String(length=300), nullable=True),
        sa.CheckConstraint("period_start <= period_end AND period_end = cut_on", name=op.f("ck_charges_period")),
        sa.CheckConstraint("subtotal >= 0 AND discount >= 0 AND discount <= subtotal", name=op.f("ck_charges_discount")),
        sa.CheckConstraint("tax >= 0 AND total >= 0", name=op.f("ck_charges_total")),
        sa.CheckConstraint("paid >= 0 AND paid <= total", name=op.f("ck_charges_paid")),
        sa.CheckConstraint("(status = 'VOID') = (voided_at IS NOT NULL)", name=op.f("ck_charges_void")),
        _company_fk("charges", "RESTRICT"),
        _catalog_fk("charges", "pricing_mode", "pricing_modes"),
        _catalog_fk("charges", "price_period", "price_periods"),
        _catalog_fk("charges", "status", "charge_statuses"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_charges")),
        sa.UniqueConstraint("company_id", "cut_on", name="uq_charges_company_cut"),
        sa.UniqueConstraint("company_id", "sequence", name="uq_charges_company_sequence"),
        schema=BILLING,
    )
    op.create_index(
        "ix_charges_open_due_on",
        "charges",
        ["due_on", "company_id"],
        unique=False,
        schema=BILLING,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.create_index("ix_charges_issued_on", "charges", ["issued_on"], unique=False, schema=BILLING)
    op.create_index("ix_charges_cut_on", "charges", ["cut_on"], unique=False, schema=BILLING)
    op.create_table(
        "charge_lines",
        sa.Column("charge_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("days", sa.SmallInteger(), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("amount", _money(), nullable=False),
        sa.CheckConstraint("days >= 0 AND units >= 0 AND amount >= 0", name=op.f("ck_charge_lines_amounts")),
        sa.ForeignKeyConstraint(
            ["charge_id"], ["billing.charges.id"], name=op.f("fk_charge_lines_charge_id_charges"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("charge_id", "month", name=op.f("pk_charge_lines")),
        schema=BILLING,
    )


def _payments() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("amount", _money(), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("reference", sa.String(length=120), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="CONFIRMED", nullable=False),
        sa.Column("applied", _money(), server_default=sa.text("0"), nullable=False),
        sa.Column("receipt_name", sa.String(length=200), nullable=True),
        sa.Column("receipt_type", sa.String(length=100), nullable=True),
        sa.Column("receipt_size", sa.Integer(), nullable=True),
        sa.Column("recorded_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by", sa.String(length=255), nullable=True),
        sa.Column("void_reason", sa.String(length=300), nullable=True),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payments_amount")),
        sa.CheckConstraint("applied >= 0 AND applied <= amount", name=op.f("ck_payments_applied")),
        sa.CheckConstraint("(status = 'VOID') = (voided_at IS NOT NULL)", name=op.f("ck_payments_void")),
        _company_fk("payments", "RESTRICT"),
        _catalog_fk("payments", "method", "payment_methods"),
        _catalog_fk("payments", "status", "payment_statuses"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        schema=BILLING,
    )
    op.create_index("ix_payments_company_paid_on", "payments", ["company_id", "paid_on", "id"], unique=False, schema=BILLING)
    op.create_index("ix_payments_paid_on", "payments", ["paid_on"], unique=False, schema=BILLING)
    op.create_index(
        "ix_payments_unapplied",
        "payments",
        ["company_id"],
        unique=False,
        schema=BILLING,
        postgresql_where=sa.text("status = 'CONFIRMED' AND applied < amount"),
    )
    op.create_table(
        "payment_receipts",
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["billing.payments.id"], name=op.f("fk_payment_receipts_payment_id_payments"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("payment_id", name=op.f("pk_payment_receipts")),
        schema=BILLING,
    )
    op.create_table(
        "payment_allocations",
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("charge_id", sa.Integer(), nullable=False),
        sa.Column("amount", _money(), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payment_allocations_amount")),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["billing.payments.id"], name=op.f("fk_payment_allocations_payment_id_payments"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["charge_id"], ["billing.charges.id"], name=op.f("fk_payment_allocations_charge_id_charges"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("payment_id", "charge_id", name="pk_payment_allocations"),
        schema=BILLING,
    )
    op.create_index("ix_payment_allocations_charge_id", "payment_allocations", ["charge_id"], unique=False, schema=BILLING)


# ---------------------------------------------------------------- consumo


def _counters() -> list[sa.Column]:
    columns = [sa.Column(name, sa.BigInteger(), server_default=sa.text("0"), nullable=False) for name in COUNTERS]
    columns.append(sa.Column("max_ms", sa.Integer(), server_default=sa.text("0"), nullable=False))
    return columns


def _usage() -> None:
    grains = (
        ("usage_daily", []),
        ("usage_routes", [sa.Column("route", sa.String(length=160), nullable=False)]),
        ("usage_users", [sa.Column("user_id", sa.Integer(), nullable=False)]),
    )
    for name, extra in grains:
        op.create_table(
            name,
            *_counters(),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            *extra,
            sa.PrimaryKeyConstraint("company_id", "day", *(c.name for c in extra), name=op.f(f"pk_{name}")),
            schema="ops",
        )
        op.create_index(f"ix_{name}_day", name, ["day"], unique=False, schema="ops")
    op.create_table(
        "storage_snapshots",
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        _catalog_fk("storage_snapshots", "category", "storage_categories"),
        sa.PrimaryKeyConstraint("company_id", "day", "category", name=op.f("pk_storage_snapshots")),
        schema="ops",
    )
    op.create_index("ix_storage_snapshots_day", "storage_snapshots", ["day"], unique=False, schema="ops")
    op.create_table(
        "daily_tasks",
        sa.Column("task", sa.String(length=30), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("task", "day", name=op.f("pk_daily_tasks")),
        schema="ops",
    )


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {BILLING}")
    _catalogs(seed)
    _menu(seed)
    _companies()
    _status_events()
    _plans()
    _charges()
    _payments()
    _usage()


def downgrade() -> None:
    for table in ("daily_tasks", "storage_snapshots", "usage_users", "usage_routes", "usage_daily"):
        op.drop_table(table, schema="ops")
    for table in ("payment_allocations", "payment_receipts", "payments", "charge_lines", "charges", "headcount_days", "plans"):
        op.drop_table(table, schema=BILLING)
    op.execute(f"DROP SCHEMA IF EXISTS {BILLING}")
    op.drop_table("employee_status_events", schema="workforce")
    with op.batch_alter_table("companies", schema="tenancy") as batch_op:
        batch_op.drop_index("ix_companies_suspended_at")
        batch_op.drop_constraint(op.f("ck_companies_suspension"), type_="check")
        batch_op.drop_constraint(op.f("fk_companies_suspension_reason_suspension_reasons"), type_="foreignkey")
        for column in ("suspended_by", "suspension_note", "suspension_reason", "suspended_at"):
            batch_op.drop_column(column)
    # Las sesiones cerradas por una suspensión conservan un motivo que sí existe antes de esta migración.
    sessions = sa.table("auth_sessions", sa.column("revoked_reason"), schema="auth")
    op.execute(sa.update(sessions).where(sessions.c.revoked_reason == REASON).values(revoked_reason="COMPANY_DEACTIVATED"))
    reasons = sa.table("session_revocation_reasons", sa.column("code"), schema="catalog")
    op.execute(sa.delete(reasons).where(reasons.c.code == REASON))
    screens = sa.table("screens", sa.column("code"), schema="catalog")
    op.execute(sa.delete(screens).where(screens.c.code.in_(SCREENS)))  # sus permisos y su módulo se van en cascada
    modules = sa.table("menu_modules", sa.column("code"), sa.column("sort_order"), schema="catalog")
    op.execute(sa.delete(modules).where(modules.c.code == MODULE))
    op.execute(
        sa.update(modules).where(modules.c.sort_order > MODULE_POSITION).values(sort_order=modules.c.sort_order - 1)
    )
    for name in reversed(CATALOGS):
        op.drop_table(name, schema="catalog")
