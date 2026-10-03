"""Domicilio y ubicación de los validadores (inicio de sesión dentro de un radio)

workforce.validators recibe el domicilio del acceso donde opera el validador (calle, número
exterior e interior, código postal, país → catalog.countries, estado, municipio y ciudad), su punto
en el mapa (latitud y longitud WGS84) y la opción "requiere ubicación" con su radio en metros: si
está activa, la cuenta solo inicia sesión a no más de ese radio del punto.

Los validadores existentes quedan sin domicilio (columnas nulas): la empresa lo completa al
editarlos. CHECKs: coordenadas juntas y en rango, radio entre 10 y 10 000 m, y exigir ubicación
requiere punto y radio.

Nuevo motivo de cierre de sesión (catalog.session_revocation_reasons): LOCATION_POLICY_CHANGED,
al exigir o cambiar la ubicación permitida de un validador con sesión abierta.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-03 12:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "workforce"
SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
NEW_REASON = "LOCATION_POLICY_CHANGED"

COLUMNS = [
    sa.Column("street", sa.String(length=150), nullable=True),
    sa.Column("exterior_number", sa.String(length=20), nullable=True),
    sa.Column("interior_number", sa.String(length=20), nullable=True),
    sa.Column("postal_code", sa.String(length=10), nullable=True),
    sa.Column("country_code", sa.String(length=2), nullable=True),
    sa.Column("state", sa.String(length=100), nullable=True),
    sa.Column("municipality", sa.String(length=100), nullable=True),
    sa.Column("city", sa.String(length=100), nullable=True),
    sa.Column("latitude", sa.Double(), nullable=True),
    sa.Column("longitude", sa.Double(), nullable=True),
    sa.Column("location_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("location_radius_m", sa.Integer(), nullable=True),
]

CHECKS = [
    (
        "ck_validators_coordinates",
        "(latitude IS NULL) = (longitude IS NULL) AND "
        "(latitude IS NULL OR (latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180))",
    ),
    ("ck_validators_location_radius", "location_radius_m IS NULL OR location_radius_m BETWEEN 10 AND 10000"),
    (
        "ck_validators_location_point",
        "NOT location_required OR (latitude IS NOT NULL AND location_radius_m IS NOT NULL)",
    ),
]


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column("validators", column, schema=SCHEMA)
    op.create_foreign_key(
        op.f("fk_validators_country_code_countries"),
        "validators",
        "countries",
        ["country_code"],
        ["code"],
        source_schema=SCHEMA,
        referent_schema="catalog",
        ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_validators_country_code"), "validators", ["country_code"], schema=SCHEMA)
    for name, condition in CHECKS:
        op.create_check_constraint(op.f(name), "validators", condition, schema=SCHEMA)

    # En una base nueva 0020 ya cargó el motivo desde el seed; en una existente se agrega aquí.
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    row = next(r for r in seed["session_revocation_reasons"] if r["code"] == NEW_REASON)
    reasons = sa.table(
        "session_revocation_reasons",
        *(sa.column(key) for key in row),
        schema="catalog",
    )
    op.execute(postgresql.insert(reasons).values(**row).on_conflict_do_nothing(index_elements=["code"]))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM catalog.session_revocation_reasons WHERE code = :code").bindparams(code=NEW_REASON))
    for name, _ in reversed(CHECKS):
        op.drop_constraint(op.f(name), "validators", schema=SCHEMA, type_="check")
    op.drop_index(op.f("ix_validators_country_code"), table_name="validators", schema=SCHEMA)
    op.drop_constraint(op.f("fk_validators_country_code_countries"), "validators", schema=SCHEMA, type_="foreignkey")
    for column in reversed(COLUMNS):
        op.drop_column("validators", column.name, schema=SCHEMA)
