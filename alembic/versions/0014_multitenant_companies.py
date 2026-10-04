"""Multiempresa: tabla companies, company_id en usuarios/empleados/registros/bitácora/política

La instalación de una sola empresa se convierte en la primera empresa de la plataforma
("Mi empresa", editable desde la consola del administrador) con todos sus datos.
Los datos únicos del empleado pasan a ser únicos POR EMPRESA y todos los índices de consulta
empiezan por company_id.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-01 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMPLOYEE_SEARCH = (
    "lower(first_name || ' ' || last_name || ' ' || employee_number || ' ' || coalesce(rfc, '')"
    " || ' ' || coalesce(curp, '') || ' ' || coalesce(nss, ''))"
)
COMPANY_SEARCH = "lower(name || ' ' || coalesce(legal_name, '') || ' ' || coalesce(rfc, ''))"
UNIQUE_PER_COMPANY = ("employee_number", "rfc", "curp", "nss")
NUMBER_INDEX = {"employee_number": "number"}


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _add_company_fk(table: str, *, nullable: bool, ondelete: str) -> None:
    op.add_column(table, sa.Column("company_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f(f"fk_{table}_company_id_companies"), table, "companies", ["company_id"], ["id"], ondelete=ondelete
    )
    if not nullable:
        op.alter_column(table, "company_id", nullable=False)


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("legal_name", sa.String(length=200), nullable=True),
        sa.Column("rfc", sa.String(length=13), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=10), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("max_employees", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
    )
    op.create_index(op.f("ix_companies_rfc"), "companies", ["rfc"], unique=True)
    op.create_index("ix_companies_name", "companies", [sa.text("lower(name)")])

    # La empresa existente (si hay usuarios) se vuelve la primera empresa de la plataforma.
    has_tenant = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM users WHERE role IN ('COMPANY', 'EMPLOYEE'))")
    ).scalar()
    company_id = None
    if has_tenant:
        company_id = bind.execute(
            sa.text("INSERT INTO companies (name, active) VALUES ('Mi empresa', true) RETURNING id")
        ).scalar()

    # ---- usuarios ----
    _add_company_fk("users", nullable=True, ondelete="RESTRICT")
    op.create_index("ix_users_company_role", "users", ["company_id", "role"])
    if company_id:
        bind.execute(
            sa.text("UPDATE users SET company_id = :c WHERE role IN ('COMPANY', 'EMPLOYEE')"), {"c": company_id}
        )

    # ---- empleados ----
    op.add_column("employees", sa.Column("company_id", sa.Integer(), nullable=True))
    if company_id:
        bind.execute(sa.text("UPDATE employees SET company_id = :c"), {"c": company_id})
    op.alter_column("employees", "company_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_employees_company_id_companies"), "employees", "companies", ["company_id"], ["id"], ondelete="RESTRICT"
    )

    # ---- registros faciales y bitácora: copia de la empresa del empleado ----
    for table in ("face_enrollments", "verification_logs"):
        op.add_column(table, sa.Column("company_id", sa.Integer(), nullable=True))
        bind.execute(
            sa.text(
                f"UPDATE {table} SET company_id = (SELECT e.company_id FROM employees e WHERE e.id = {table}.employee_id)"
            )
        )
    op.alter_column("face_enrollments", "company_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_face_enrollments_company_id_companies"),
        "face_enrollments",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_verification_logs_company_id_companies"),
        "verification_logs",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # ---- política: una por empresa (la actual pasa a la primera empresa) ----
    op.add_column("verification_policy", sa.Column("company_id", sa.Integer(), nullable=True))
    if company_id:
        bind.execute(
            sa.text(
                "UPDATE verification_policy SET company_id = :c WHERE id = (SELECT min(id) FROM verification_policy)"
            ),
            {"c": company_id},
        )
    bind.execute(sa.text("DELETE FROM verification_policy WHERE company_id IS NULL"))
    op.alter_column("verification_policy", "company_id", nullable=False)
    op.create_index(op.f("ix_verification_policy_company_id"), "verification_policy", ["company_id"], unique=True)
    op.create_foreign_key(
        op.f("fk_verification_policy_company_id_companies"),
        "verification_policy",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # ---- índices: todos empiezan por company_id ----
    for column in UNIQUE_PER_COMPANY:
        op.drop_index(f"ix_employees_{column}", table_name="employees")
        name = NUMBER_INDEX.get(column, column)
        op.create_index(f"uq_employees_company_{name}", "employees", ["company_id", column], unique=True)
    op.drop_index("ix_employees_name_order", table_name="employees")
    op.create_index("ix_employees_company_name", "employees", ["company_id", "last_name", "first_name", "id"])
    op.drop_index("ix_face_enrollments_status_submitted", table_name="face_enrollments")
    op.create_index(
        "ix_face_enrollments_company_status", "face_enrollments", ["company_id", "status", "submitted_at", "id"]
    )
    op.create_index("ix_verification_logs_company_created", "verification_logs", ["company_id", "created_at"])

    if _postgres():
        # GIN compuesto (empresa + trigramas): la búsqueda de una empresa no toca a las demás.
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gin")
        op.execute("DROP INDEX IF EXISTS ix_employees_search_trgm")
        op.execute(
            "CREATE INDEX ix_employees_company_search_trgm ON employees "
            f"USING gin (company_id, ({EMPLOYEE_SEARCH}) gin_trgm_ops)"
        )
        op.execute(f"CREATE INDEX ix_companies_search_trgm ON companies USING gin (({COMPANY_SEARCH}) gin_trgm_ops)")
        op.execute("ANALYZE employees")
        op.execute("ANALYZE companies")


def downgrade() -> None:
    if _postgres():
        op.execute("DROP INDEX IF EXISTS ix_companies_search_trgm")
        op.execute("DROP INDEX IF EXISTS ix_employees_company_search_trgm")
        op.execute(f"CREATE INDEX ix_employees_search_trgm ON employees USING gin (({EMPLOYEE_SEARCH}) gin_trgm_ops)")
    op.drop_index("ix_verification_logs_company_created", table_name="verification_logs")
    op.drop_index("ix_face_enrollments_company_status", table_name="face_enrollments")
    op.create_index("ix_face_enrollments_status_submitted", "face_enrollments", ["status", "submitted_at", "id"])
    op.drop_index("ix_employees_company_name", table_name="employees")
    op.create_index("ix_employees_name_order", "employees", ["last_name", "first_name", "id"])
    for column in UNIQUE_PER_COMPANY:
        name = NUMBER_INDEX.get(column, column)
        op.drop_index(f"uq_employees_company_{name}", table_name="employees")
        op.create_index(f"ix_employees_{column}", "employees", [column], unique=True)
    op.drop_constraint(op.f("fk_verification_policy_company_id_companies"), "verification_policy", type_="foreignkey")
    op.drop_index(op.f("ix_verification_policy_company_id"), table_name="verification_policy")
    op.drop_column("verification_policy", "company_id")
    for table in ("verification_logs", "face_enrollments", "employees"):
        op.drop_constraint(op.f(f"fk_{table}_company_id_companies"), table, type_="foreignkey")
        op.drop_column(table, "company_id")
    op.drop_index("ix_users_company_role", table_name="users")
    op.drop_constraint(op.f("fk_users_company_id_companies"), "users", type_="foreignkey")
    op.drop_column("users", "company_id")
    op.drop_index("ix_companies_name", table_name="companies")
    op.drop_index(op.f("ix_companies_rfc"), table_name="companies")
    op.drop_table("companies")
