"""Departamentos de cada empresa, con responsables y empleados asignados

- `workforce.departments`: nombre único por empresa (sin distinguir mayúsculas) y descripción.
- `workforce.employees.department_id`: cada empleado está a lo más en un departamento. La llave
  foránea compuesta (departamento, empresa) garantiza en la BD que sea de SU empresa, y RESTRICT
  impide borrar un departamento que todavía tiene empleados.
- `workforce.department_managers`: responsables (varios por departamento), empleados de la misma
  empresa (dos llaves compuestas); se van con su departamento o con el empleado.
- Pantalla `COMPANY_DEPARTMENTS` (menú de COMPANY, después de Empleados) y su permiso.

No modifica datos existentes: ningún empleado queda asignado hasta que la empresa lo decida.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-04 00:30:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
SCREEN = "COMPANY_DEPARTMENTS"
SCREEN_ORDER = 5


def _departments() -> None:
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_departments_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_departments")),
        sa.UniqueConstraint("id", "company_id", name="uq_departments_id_company"),
        schema="workforce",
    )
    op.create_index(
        "uq_departments_company_name",
        "departments",
        ["company_id", sa.text("lower(name)")],
        unique=True,
        schema="workforce",
    )


def _managers() -> None:
    op.create_table(
        "department_managers",
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["department_id", "company_id"],
            ["workforce.departments.id", "workforce.departments.company_id"],
            name="fk_department_managers_department_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id", "company_id"],
            ["workforce.employees.id", "workforce.employees.company_id"],
            name="fk_department_managers_employee_company",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("department_id", "employee_id", name=op.f("pk_department_managers")),
        schema="workforce",
    )
    op.create_index("ix_department_managers_employee", "department_managers", ["employee_id"], schema="workforce")


def _employee_department() -> None:
    op.add_column("employees", sa.Column("department_id", sa.Integer(), nullable=True), schema="workforce")
    op.create_foreign_key(
        "fk_employees_department_company",
        "employees",
        "departments",
        ["department_id", "company_id"],
        ["id", "company_id"],
        source_schema="workforce",
        referent_schema="workforce",
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_employees_company_department",
        "employees",
        ["company_id", "department_id", "last_name", "first_name", "id"],
        schema="workforce",
        postgresql_where=sa.text("department_id IS NOT NULL"),
    )


def _screen() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    for row in seed["screens"]:  # orden del menú del seed: Departamentos va después de Empleados
        op.execute(sa.update(screens).where(screens.c.code == row["code"]).values(sort_order=row["sort_order"]))
    screen = next(r for r in seed["screens"] if r["code"] == SCREEN)
    table = sa.table("screens", *(sa.column(key) for key in screen), schema="catalog")
    op.execute(postgresql.insert(table).values(**screen).on_conflict_do_nothing(index_elements=["code"]))
    grants = sa.table("role_screens", sa.column("role_code"), sa.column("screen_code"), schema="catalog")
    for grant in (g for g in seed["role_screens"] if g["screen_code"] == SCREEN):
        op.execute(
            postgresql.insert(grants)
            .values(**grant)
            .on_conflict_do_nothing(index_elements=["role_code", "screen_code"])
        )


def upgrade() -> None:
    _departments()
    _managers()
    _employee_department()
    _screen()


def downgrade() -> None:
    grants = sa.table("role_screens", sa.column("screen_code"), schema="catalog")
    op.execute(sa.delete(grants).where(grants.c.screen_code == SCREEN))
    screens = sa.table("screens", sa.column("code"), sa.column("sort_order"), schema="catalog")
    op.execute(sa.delete(screens).where(screens.c.code == SCREEN))
    op.execute(
        sa.update(screens).where(screens.c.sort_order > SCREEN_ORDER).values(sort_order=screens.c.sort_order - 1)
    )
    op.drop_index("ix_employees_company_department", table_name="employees", schema="workforce")
    op.drop_constraint("fk_employees_department_company", "employees", type_="foreignkey", schema="workforce")
    op.drop_column("employees", "department_id", schema="workforce")
    op.drop_index("ix_department_managers_employee", table_name="department_managers", schema="workforce")
    op.drop_table("department_managers", schema="workforce")
    op.drop_index("uq_departments_company_name", table_name="departments", schema="workforce")
    op.drop_table("departments", schema="workforce")
