"""Documentos de identidad del empleado (onboarding con OCR; decisión del dueño del producto, 2026-10-07)

«En el onboarding se debe solicitar comprobante de domicilio e identificación oficial (pasaporte, INE); se debe extraer
la información y mostrarla en el expediente del empleado para que la empresa apruebe o rechace al empleado» (README,
«Documentos del empleado (onboarding con OCR)»; diseño en `docs/rd/documentos-ocr-2026-10-07.md`):

- `catalog.employee_document_types` (nuevo): Pasaporte, Credencial para votar (INE), Licencia de conducir, Otra
  identificación oficial y Comprobante de domicilio, con sus traducciones a los seis idiomas (las carga esta migración,
  como la 0081: la 0064 y la 0078 saltan los catálogos que aún no existían).
- `workforce.employee_documents` (nueva, de empresa, con borrado lógico): la REFERENCIA de cada archivo (objeto cifrado
  en el bucket, nunca sus bytes) MÁS los datos que el OCR extrajo (texto que la empresa confirma o corrige; el número de
  documento cifrado en reposo). Seguridad por fila como toda tabla de empresa; FK compuesta `(employee_id, company_id)`
  al empleado (CASCADE). Índices: `ix_employee_documents_employee` (`company_id, employee_id, id` con `deleted_at` en el
  INCLUDE: el listado por empleado, su conteo y la FK compuesta) e `ix_employee_documents_deleted` (parcial: la papelera
  y la depuración). Su tipo es una FK a un catálogo que nunca se borra: sin índice (§3.1.8). Pequeña (pocos documentos
  por empleado): no se particiona.
- `tenancy.companies.require_employee_documents` (nueva, por omisión false): el interruptor por empresa que decide el
  ADMIN (como `api_enabled`). Encendido, el empleado sube documentos en el onboarding y la empresa los revisa.
- Pantalla `EMPLOYEE_DOCUMENTS` («Documentos», rol EMPLOYEE, módulo «Identidad») con sus traducciones.

Compatible con la versión anterior en marcha (solo agrega: columna con valor por omisión, tablas y una pantalla). Los
permisos de la API sobre la tabla nueva los pone `python -m app.cli db roles` en el mismo despliegue (servicio
`migrate`). `downgrade` quita la tabla (sus objetos del bucket quedan sin referencia: se borran con la regla de ciclo
de vida o a mano), la columna, la pantalla, el catálogo y sus traducciones.

Revision ID: 0087
Revises: 0086
Create Date: 2026-10-07 12:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0087"
down_revision: str | None = "0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
CATALOG = "catalog"
TENANCY = "tenancy"
WORKFORCE = "workforce"
TYPES = "employee_document_types"
TABLE = "employee_documents"
SCREEN = "EMPLOYEE_DOCUMENTS"
#: La seguridad por fila de la tabla nueva (la misma SQL que `row_security.policy_ddl`), en sentencias constantes.
ROW_SECURITY = (
    "ALTER TABLE workforce.employee_documents ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE workforce.employee_documents FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON workforce.employee_documents "
    "USING (company_id = NULLIF(current_setting('app.company_id', true), '')::integer) "
    "WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), '')::integer)",
    "COMMENT ON COLUMN workforce.employee_documents.company_id IS 'Empresa dueña de la fila. Seguridad por fila "
    "(política tenant_isolation): solo se ve y se escribe en una transacción con app.company_id igual (o con el rol de "
    "la plataforma).'",
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


def _insert_missing(table_name: str, row: dict, keys: list[str]) -> None:
    """Inserta la fila del seed si no está (idempotente: una base nueva ya cargó la pantalla con el seed vigente)."""
    table = sa.table(table_name, *(sa.column(key) for key in row), schema=CATALOG)
    op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=keys))


def _types(seed: dict) -> None:
    table = op.create_table(
        TYPES,
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f(f"pk_{TYPES}")),
        schema=CATALOG,
    )
    op.bulk_insert(table, seed[TYPES])


def _screen(seed: dict) -> None:
    for row in (r for r in seed["screens"] if r["code"] == SCREEN):
        _insert_missing("screens", row, ["code"])
    for grant in (g for g in seed["role_screens"] if g["screen_code"] == SCREEN):
        _insert_missing("role_screens", grant, ["role_code", "screen_code"])
    for link in (m for m in seed["menu_module_screens"] if m["screen_code"] == SCREEN):
        _insert_missing("menu_module_screens", link, ["screen_code"])


def _translations() -> None:
    """Los textos del catálogo nuevo y de la pantalla en cada idioma con archivo (`catalogs.<idioma>.json`)."""
    rows: list[dict] = []
    for path in sorted(SEED_DIR.glob("catalogs.*.json")):
        locale = path.name.removeprefix("catalogs.").removesuffix(".json")
        texts = json.loads(path.read_text(encoding="utf-8"))
        rows += [
            {"catalog": TYPES, "code": code, "locale": locale, "field": field, "text": text}
            for code, fields in texts.get(TYPES, {}).items()
            for field, text in fields.items()
        ]
        rows += [
            {"catalog": "screens", "code": SCREEN, "locale": locale, "field": field, "text": text}
            for field, text in texts.get("screens", {}).get(SCREEN, {}).items()
        ]
    if not rows:
        return
    statement = postgresql.insert(_TRANSLATIONS).values(rows)
    op.execute(
        statement.on_conflict_do_update(
            index_elements=["catalog", "code", "locale", "field"], set_={"text": statement.excluded.text}
        )
    )


def _documents() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("file_name", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("uid", sa.String(length=32), nullable=False),
        sa.Column("object_name", sa.String(length=300), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_by", sa.String(length=255), nullable=False),
        sa.Column("uploaded_by_employee", sa.Boolean(), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("document_number_encrypted", sa.String(length=255), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("nationality", sa.String(length=3), nullable=True),
        sa.Column("sex", sa.String(length=1), nullable=True),
        sa.Column("curp", sa.String(length=18), nullable=True),
        sa.Column("voter_key", sa.String(length=20), nullable=True),
        sa.Column("postal_code", sa.String(length=10), nullable=True),
        sa.Column("address", sa.String(length=300), nullable=True),
        sa.Column("ocr_processed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("mrz_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confirmed_by", sa.String(length=255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(length=255), nullable=True),
        # El empleado y la empresa van juntos: la base rechaza ligar un documento a un empleado de otra empresa.
        sa.ForeignKeyConstraint(
            ["employee_id", "company_id"],
            [f"{WORKFORCE}.employees.id", f"{WORKFORCE}.employees.company_id"],
            name="fk_employee_documents_employee_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["type"],
            [f"{CATALOG}.{TYPES}.code"],
            name=op.f("fk_employee_documents_type_employee_document_types"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_documents")),
        schema=WORKFORCE,
    )
    op.create_index(
        "ix_employee_documents_employee",
        TABLE,
        ["company_id", "employee_id", "id"],
        schema=WORKFORCE,
        postgresql_include=["deleted_at"],
    )
    op.create_index(
        "ix_employee_documents_deleted",
        TABLE,
        ["company_id", "employee_id", "deleted_at", "id"],
        schema=WORKFORCE,
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )
    for statement in ROW_SECURITY:
        op.execute(statement)


def upgrade() -> None:
    seed = _seed("catalogs.json")
    _types(seed)
    _screen(seed)
    _translations()
    op.add_column(
        "companies",
        sa.Column("require_employee_documents", sa.Boolean(), server_default=sa.false(), nullable=False),
        schema=TENANCY,
    )
    _documents()
    op.get_bind().exec_driver_sql((SQL_DIR / "0087_comments.sql").read_text(encoding="utf-8").replace("%", "%%"))


def downgrade() -> None:
    op.drop_table(TABLE, schema=WORKFORCE)
    op.drop_column("companies", "require_employee_documents", schema=TENANCY)
    op.execute(sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.catalog == TYPES))
    op.execute(sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.catalog == "screens", _TRANSLATIONS.c.code == SCREEN))
    for table, column in (("menu_module_screens", "screen_code"), ("role_screens", "screen_code"), ("screens", "code")):
        rows = sa.table(table, sa.column(column), schema=CATALOG)
        op.execute(sa.delete(rows).where(rows.c[column] == SCREEN))
    op.drop_table(TYPES, schema=CATALOG)
