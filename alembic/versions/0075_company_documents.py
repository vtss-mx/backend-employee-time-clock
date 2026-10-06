"""Documentos de la empresa (decisión del dueño del producto, 2026-10-06)

«Implementa un mecanismo para poder guardar archivos PDF, Word, etc. relacionados con la empresa para fines de emitir
facturas en el futuro» y «todos los archivos, imágenes, se deben guardar en Firebase» (README, "Documentos de la
empresa"):

- `catalog.company_document_types` (nuevo): Constancia de situación fiscal, Acta constitutiva, Comprobante de domicilio,
  Identificación del representante legal, Contrato y Otro, con su traducción a en-US. La migración 0064 ya salta los
  catálogos que todavía no existen en una base nueva (corrección de la 0074): sus registros y sus traducciones los
  carga esta migración.
- `tenancy.company_documents` (nueva, de empresa, con borrado lógico): la REFERENCIA de cada archivo (objeto, tipo,
  tamaño, SHA-256 y cuándo se subió), nunca sus bytes: el archivo va CIFRADO al bucket (`STORED_IMAGES`). Seguridad
  por fila como toda tabla de empresa; `ix_company_documents_company` (`company_id, id` con `deleted_at` en el INCLUDE:
  el listado de lo vigente, el más reciente primero, su conteo y la FK hacia la empresa) e `ix_company_documents_deleted`
  (parcial: la papelera y la depuración). Su tipo es una FK a un catálogo que nunca se borra: sin índice (§3.1.8).
  Pequeña (unos cuantos documentos por empresa): no se particiona.
- Pantalla `COMPANY_DOCUMENTS` («Documentos», rol COMPANY, módulo «Cuenta») con su traducción.
- `storage_categories.BILLING` dice ahora que también cuenta los documentos de la empresa (la foto diaria del
  almacenamiento suma el tamaño real de sus archivos).

Tabla nueva: el código anterior en marcha no la conoce (compatible con la versión anterior). Los permisos de la API
sobre la tabla los pone `python -m app.cli db roles` en el mismo despliegue (servicio `migrate`).

`downgrade` quita la tabla (sus objetos del bucket quedan sin referencia: se borran con la regla de ciclo de vida o a
mano), la pantalla, el catálogo y sus traducciones, y regresa el texto anterior de la categoría.

Revision ID: 0075
Revises: 0074
Create Date: 2026-10-06 18:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
CATALOG = "catalog"
TENANCY = "tenancy"
LOCALE = "en-US"
TYPES = "company_document_types"
TABLE = "company_documents"
SCREEN = "COMPANY_DOCUMENTS"
CATEGORY = "BILLING"
#: El texto de la categoría del almacenamiento antes de esta migración (para el `downgrade`).
PREVIOUS_CATEGORY = {"es": "Cargos, pagos y comprobantes.", "en": "Charges, payments, and receipts."}
#: La seguridad por fila de la tabla nueva (la misma SQL que `row_security.policy_ddl`), en sentencias constantes.
ROW_SECURITY = (
    "ALTER TABLE tenancy.company_documents ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE tenancy.company_documents FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON tenancy.company_documents "
    "USING (company_id = NULLIF(current_setting('app.company_id', true), '')::integer) "
    "WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), '')::integer)",
    "COMMENT ON COLUMN tenancy.company_documents.company_id IS 'Empresa dueña de la fila. Seguridad por fila (política "
    "tenant_isolation): solo se ve y se escribe en una transacción con app.company_id igual (o con el rol de la "
    "plataforma).'",
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
_CATEGORIES = sa.table("storage_categories", sa.column("code"), sa.column("description"), schema=CATALOG)


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


def _menu(seed: dict) -> None:
    for row in (r for r in seed["screens"] if r["code"] == SCREEN):
        _insert_missing("screens", row, ["code"])
    for grant in (g for g in seed["role_screens"] if g["screen_code"] == SCREEN):
        _insert_missing("role_screens", grant, ["role_code", "screen_code"])
    for link in (m for m in seed["menu_module_screens"] if m["screen_code"] == SCREEN):
        _insert_missing("menu_module_screens", link, ["screen_code"])


def _translations(english: dict) -> None:
    """Los textos en inglés del catálogo nuevo, de la pantalla y de la categoría (inserción o actualización)."""
    rows = [
        {"catalog": TYPES, "code": code, "locale": LOCALE, "field": field, "text": text}
        for code, fields in english[TYPES].items()
        for field, text in fields.items()
    ]
    rows += [
        {"catalog": "screens", "code": SCREEN, "locale": LOCALE, "field": field, "text": text}
        for field, text in english["screens"][SCREEN].items()
    ]
    rows.append(
        {
            "catalog": "storage_categories",
            "code": CATEGORY,
            "locale": LOCALE,
            "field": "description",
            "text": english["storage_categories"][CATEGORY]["description"],
        }
    )
    statement = postgresql.insert(_TRANSLATIONS).values(rows)
    op.execute(
        statement.on_conflict_do_update(
            index_elements=["catalog", "code", "locale", "field"], set_={"text": statement.excluded.text}
        )
    )


def _category(spanish: str, english: str) -> None:
    op.execute(sa.update(_CATEGORIES).where(_CATEGORIES.c.code == CATEGORY).values(description=spanish))
    op.execute(
        sa.update(_TRANSLATIONS)
        .where(
            _TRANSLATIONS.c.catalog == "storage_categories",
            _TRANSLATIONS.c.code == CATEGORY,
            _TRANSLATIONS.c.locale == LOCALE,
            _TRANSLATIONS.c.field == "description",
        )
        .values(text=english)
    )


def _documents() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("file_name", sa.String(length=200), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("uid", sa.String(length=32), nullable=False),
        sa.Column("object_name", sa.String(length=300), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_by", sa.String(length=255), nullable=False),
        sa.Column("uploaded_by_platform", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            [f"{TENANCY}.companies.id"],
            name=op.f("fk_company_documents_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["type"],
            [f"{CATALOG}.{TYPES}.code"],
            name=op.f("fk_company_documents_type_company_document_types"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_documents")),
        schema=TENANCY,
    )
    op.create_index(
        "ix_company_documents_company",
        TABLE,
        ["company_id", "id"],
        schema=TENANCY,
        postgresql_include=["deleted_at"],
    )
    op.create_index(
        "ix_company_documents_deleted",
        TABLE,
        ["company_id", "deleted_at", "id"],
        schema=TENANCY,
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )
    for statement in ROW_SECURITY:
        op.execute(statement)


def upgrade() -> None:
    seed, english = _seed("catalogs.json"), _seed(f"catalogs.{LOCALE}.json")
    _types(seed)
    _menu(seed)
    _translations(english)
    category = next(row for row in seed["storage_categories"] if row["code"] == CATEGORY)
    _category(category["description"], english["storage_categories"][CATEGORY]["description"])
    _documents()
    op.get_bind().exec_driver_sql((SQL_DIR / "0075_comments.sql").read_text(encoding="utf-8").replace("%", "%%"))


def downgrade() -> None:
    op.drop_table(TABLE, schema=TENANCY)
    op.execute(sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.catalog == TYPES))
    op.execute(sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.catalog == "screens", _TRANSLATIONS.c.code == SCREEN))
    _category(PREVIOUS_CATEGORY["es"], PREVIOUS_CATEGORY["en"])
    for table, column in (("menu_module_screens", "screen_code"), ("role_screens", "screen_code"), ("screens", "code")):
        rows = sa.table(table, sa.column(column), schema=CATALOG)
        op.execute(sa.delete(rows).where(rows.c[column] == SCREEN))
    op.drop_table(TYPES, schema=CATALOG)
