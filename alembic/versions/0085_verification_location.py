"""Ubicación de las verificaciones de identidad (decisión del dueño del producto, 2026-10-07)

«Cuando se haga el proceso de verificación se debe enviar la geolocalización, esto es obligatorio, y como empresa debo
poder ver esa información en el mapa de Google Maps para ver dónde se hizo la verificación» (contrato
`docs/sdk/contrato-verificacion.md` 1.3.0; README «Ubicación de las verificaciones»):

- `attendance.verification_logs`: columnas `latitude`, `longitude` y `location_accuracy_m` (nulas: compatibles con el
  código anterior y con las filas existentes). Sin índice: el listado de la empresa filtra por empresa y fecha (índices
  existentes) y nada consulta por lat/lng. La tabla es particionada: `ADD COLUMN` nula es metadato (instantáneo) y se
  propaga a todas las particiones.
- `tenancy.verification_policy`: columna `verification_location` (modo de `catalog.signal_modes`): OFF no la pide,
  OBSERVE la registra (la empresa la ve en el mapa) y ENFORCE la exige (sin ubicación válida el servidor no completa la
  verificación, antes del motor). Por omisión OBSERVE (registra sin dejar a nadie afuera; el ADMIN la sube a ENFORCE a
  propósito). NOT NULL con server_default: las filas existentes toman OBSERVE. FK a un catálogo que nunca se borra: sin
  índice (§3.1.8).
- Pantalla `COMPANY_VERIFICATIONS` («Verificaciones», rol COMPANY, módulo «Asistencia») con sus siete traducciones: la
  empresa ve en el mapa dónde y cuándo se verificó la identidad de su gente.

Compatible con la versión anterior (agrega, no quita): columnas nulas o con valor por omisión y una pantalla que el
código viejo ignora (su endpoint solo existe en el código nuevo). Nada que depurar.

`downgrade`: quita las columnas de la bitácora, la columna de la política (con su FK) y la pantalla con sus traducciones.

Revision ID: 0085
Revises: 0084
Create Date: 2026-10-07 20:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
TENANCY = "tenancy"
ATTENDANCE = "attendance"
SCREEN = "COMPANY_VERIFICATIONS"
POLICY_FK = op.f("fk_verification_policy_verification_location_signal_modes")

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
    """Inserta la fila del seed si no está (idempotente: una base nueva ya la cargó con el seed vigente)."""
    table = sa.table(table_name, *(sa.column(key) for key in row), schema=CATALOG)
    op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=keys))


def _menu(seed: dict) -> None:
    for row in (r for r in seed["screens"] if r["code"] == SCREEN):
        _insert_missing("screens", row, ["code"])
    for grant in (g for g in seed["role_screens"] if g["screen_code"] == SCREEN):
        _insert_missing("role_screens", grant, ["role_code", "screen_code"])
    for link in (m for m in seed["menu_module_screens"] if m["screen_code"] == SCREEN):
        _insert_missing("menu_module_screens", link, ["screen_code"])


def _translations() -> None:
    """Los textos de la pantalla nueva en cada idioma con archivo de traducciones (en-US, pt-BR, fr-FR, de-DE, it-IT,
    es-ES)."""
    rows: list[dict[str, str]] = []
    for path in sorted(SEED_DIR.glob("catalogs.*.json")):
        locale = path.name.removeprefix("catalogs.").removesuffix(".json")
        texts = json.loads(path.read_text(encoding="utf-8"))
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


def upgrade() -> None:
    # Bitácora: dónde se hizo cada verificación (nulas; sin índice: el listado filtra por empresa y fecha).
    op.add_column("verification_logs", sa.Column("latitude", sa.Float(), nullable=True), schema=ATTENDANCE)
    op.add_column("verification_logs", sa.Column("longitude", sa.Float(), nullable=True), schema=ATTENDANCE)
    op.add_column("verification_logs", sa.Column("location_accuracy_m", sa.Integer(), nullable=True), schema=ATTENDANCE)
    # Política: el modo de la ubicación de las verificaciones (por omisión OBSERVE).
    op.add_column(
        "verification_policy",
        sa.Column("verification_location", sa.String(length=20), server_default="OBSERVE", nullable=False),
        schema=TENANCY,
    )
    op.create_foreign_key(
        POLICY_FK,
        "verification_policy",
        "signal_modes",
        ["verification_location"],
        ["code"],
        source_schema=TENANCY,
        referent_schema=CATALOG,
    )
    # Pantalla «Verificaciones» con sus siete traducciones (su español vive en las columnas del seed).
    _menu(_seed("catalogs.json"))
    _translations()


def downgrade() -> None:
    op.execute(sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.catalog == "screens", _TRANSLATIONS.c.code == SCREEN))
    for table, column in (("menu_module_screens", "screen_code"), ("role_screens", "screen_code"), ("screens", "code")):
        rows = sa.table(table, sa.column(column), schema=CATALOG)
        op.execute(sa.delete(rows).where(rows.c[column] == SCREEN))
    op.drop_constraint(POLICY_FK, "verification_policy", schema=TENANCY, type_="foreignkey")
    op.drop_column("verification_policy", "verification_location", schema=TENANCY)
    for column in ("location_accuracy_m", "longitude", "latitude"):
        op.drop_column("verification_logs", column, schema=ATTENDANCE)
