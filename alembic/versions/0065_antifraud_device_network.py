"""Antifraude de identidad, fase 1b: dispositivo del empleado, red (base local DB-IP), lugar, 1:N en cada 1:1 y
telemetría del navegador

Lo que la fase 1 dejó pendiente (`docs/rd/antifraude-identidad.md` §2.5-§2.7, §4.2 y §7.1), con las decisiones del
dueño del producto: D2 (modos del dispositivo del empleado: apagado, solo medir, un paso más o aprobación de la
empresa; por omisión solo medir), D8 (base local de IP DB-IP Lite, CC BY 4.0: la IP nunca sale del servidor) y D10 (la
empresa revisa los registros "en revisión"). Toda señal nueva nace en «Solo medir» y ninguna niega
(`risk_rules.ASK_ONLY_SIGNALS`): como mucho pide un paso más o deja el registro "en revisión".

- `workforce.employee_devices` (nueva, de empresa): cada dispositivo desde el que un empleado checa o verifica su
  identidad, por el HASH de la llave no exportable que la app genera en el navegador (nunca la llave ni datos del
  navegador más que un nombre amigable), su decisión de la empresa (`device_statuses`), primer y último uso, usos y
  cuándo superó un paso más. Acotada por empleados × dispositivos (se va con el empleado: CASCADE), no crece con el
  tiempo: no se particiona. Su único `(company_id, key_hash, employee_id)` sirve a la inserción atómica de cada uso y,
  por su prefijo, a "¿quién más usó esta llave?" (DEVICE_SHARED); el índice `(company_id, employee_id, first_seen_at,
  id)` a la lista de cada empleado y a su FK compuesta. `fillfactor` 90: cada uso actualiza su fila (HOT). Seguridad
  por fila como toda tabla de empresa.
- `attendance.attendance_events`: `ip_country` e `ip_asn` (de la base local, nunca la IP) para comparar la red de un
  registro con la del anterior (NETWORK_JUMP). Columnas nulas sin valor por omisión en la tabla particionada: solo
  catálogo, sin reescribirla.
- Catálogos: 16 señales nuevas del motor de riesgo (`risk_signals`, todas en «Solo medir»), los motivos de negocio
  DEVICE, NETWORK y BROWSER (`review_reasons`), y textos al día (motivos de cámara, identidad y lugar; los modos del
  dispositivo, que ya hacen algo; los niveles predefinidos, que dicen qué hacen con el dispositivo). Cada texto con su
  traducción a en-US (`catalog.translations`, inserción o actualización idempotente: una base nueva ya los trae del
  seed vigente).

`downgrade` quita la tabla y las columnas, borra las señales y motivos nuevos (con su línea base y sus traducciones) y
deja los textos como estaban en los dos idiomas.

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-05 12:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
CATALOG = "catalog"
WORKFORCE = "workforce"
ATTENDANCE = "attendance"
LOCALE = "en-US"
TABLE = "employee_devices"
EXPRESSION = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
COMPANY_COMMENT = (
    "Empresa dueña de la fila. Seguridad por fila (política tenant_isolation): solo se ve y se escribe en una "
    "transacción con app.company_id igual (o con el rol de la plataforma)."
)
NEW_REVIEW_REASONS = ("DEVICE", "NETWORK", "BROWSER")
NEW_SIGNALS = (
    "DEVICE_NEW",
    "DEVICE_KEY_MISSING",
    "DEVICE_SHARED",
    "IDENTITY_MISMATCH",
    "NETWORK_HOSTING",
    "NETWORK_COUNTRY_MISMATCH",
    "NETWORK_JUMP",
    "LOCATION_STATIC",
    "LOCATION_JUMP",
    "AUTOMATION",
    "VIRTUAL_CAMERA_PRESENT",
    "TRACK_INCONSISTENT",
    "FRAME_TIMING_SYNTHETIC",
    "SCREEN_INCOHERENT",
    "TELEMETRY_MISSING",
    "JPEG_TABLE_UNKNOWN",
)
#: Textos que cambian: (catálogo, código, columna) → (español anterior, inglés anterior). El nuevo sale del seed.
PREVIOUS_TEXTS = {
    ("review_reasons", "CAMERA", "description"): (
        "La captura no informó de qué cámara venía.",
        "The capture didn't report which camera it came from.",
    ),
    ("review_reasons", "IDENTITY", "description"): (
        "El rostro coincidió con poca holgura.",
        "The face matched with little margin.",
    ),
    ("review_reasons", "LOCATION", "description"): (
        "La ubicación parece simulada o quedó en el límite del sitio.",
        "The location looks simulated or was at the edge of the site.",
    ),
    ("employee_device_modes", "OBSERVE", "description"): (
        "Se registra desde qué dispositivo checa cada empleado, sin pedir nada más.",
        "It records which device each employee checks in from, without asking for anything else.",
    ),
    ("employee_device_modes", "STEP_UP", "description"): (
        "Un dispositivo nuevo pide un reto más exigente.",
        "A new device asks for a tougher challenge.",
    ),
    ("employee_device_modes", "APPROVAL", "description"): (
        "Cada dispositivo nuevo lo debe aprobar la empresa, como los de los validadores.",
        "The company must approve each new device, like the validators' devices.",
    ),
    ("policy_presets", "STANDARD", "description"): (
        "Para la mayoría: dos movimientos en 60 s, destello en Solo medir hasta calibrarlo, anti-spoofing Estándar y "
        "cortes de riesgo 30/60/80.",
        "For most companies: two movements in 60 s, flash in Measure only until calibrated, Standard anti-spoofing, "
        "and risk cutoffs 30/60/80.",
    ),
    ("policy_presets", "HIGH", "description"): (
        "Más exigente: tres movimientos en 45 s, destello obligatorio, anti-spoofing Alto, reenvío perceptual "
        "obligatorio y cortes 25/50/75.",
        "Stricter: three movements in 45 s, required flash, High anti-spoofing, required perceptual resubmission "
        "check, and cutoffs 25/50/75.",
    ),
    ("policy_presets", "MAXIMUM", "description"): (
        "Para sitios con fraude confirmado: tres movimientos en 30 s, anti-spoofing Máximo, reglas duras obligatorias "
        "y el riesgo alto se niega. Más reintentos con poca luz.",
        "For sites with confirmed fraud: three movements in 30 s, Maximum anti-spoofing, required hard rules, and high "
        "risk is denied. More retries in low light.",
    ),
}
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


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _rows(seed: dict, catalog: str, codes: Sequence[str]) -> list[dict]:
    return [row for row in seed[catalog] if row["code"] in codes]


def _catalog_rows(seed: dict) -> None:
    """Los motivos y las señales nuevos (idempotente: una base nueva ya los cargó con el seed vigente)."""
    for catalog, codes in (("review_reasons", NEW_REVIEW_REASONS), ("risk_signals", NEW_SIGNALS)):
        items = _rows(seed, catalog, codes)
        table = sa.table(catalog, *(sa.column(key) for key in items[0]), schema=CATALOG)
        op.execute(postgresql.insert(table).values(items).on_conflict_do_nothing(index_elements=["code"]))


def _texts(seed: dict, english: dict) -> None:
    """Los textos al día en español (columnas del catálogo) y en inglés (`catalog.translations`)."""
    bind = op.get_bind()
    for catalog, code, column in PREVIOUS_TEXTS:
        text = next(row[column] for row in seed[catalog] if row["code"] == code)
        bind.execute(
            sa.text(f"UPDATE {CATALOG}.{catalog} SET {column} = :text WHERE code = :code"), {"text": text, "code": code}
        )
    rows = [
        {"catalog": catalog, "code": code, "locale": LOCALE, "field": field, "text": text}
        for catalog, codes in (("review_reasons", NEW_REVIEW_REASONS), ("risk_signals", NEW_SIGNALS))
        for code in codes
        for field, text in english[catalog][code].items()
    ]
    rows += [
        {"catalog": catalog, "code": code, "locale": LOCALE, "field": column, "text": english[catalog][code][column]}
        for catalog, code, column in PREVIOUS_TEXTS
    ]
    statement = postgresql.insert(_TRANSLATIONS).values(rows)
    op.execute(
        statement.on_conflict_do_update(
            index_elements=["catalog", "code", "locale", "field"], set_={"text": statement.excluded.text}
        )
    )


def _devices() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("uses", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("stepped_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_employee_devices_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id", "company_id"],
            [f"{WORKFORCE}.employees.id", f"{WORKFORCE}.employees.company_id"],
            name="fk_employee_devices_employee_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["status"], [f"{CATALOG}.device_statuses.code"], name=op.f("fk_employee_devices_status_device_statuses")
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"],
            ["auth.users.id"],
            name=op.f("fk_employee_devices_reviewed_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_devices")),
        sa.UniqueConstraint("company_id", "key_hash", "employee_id", name="uq_employee_devices_key"),
        schema=WORKFORCE,
        postgresql_with={"fillfactor": 90},
    )
    op.create_index(
        "ix_employee_devices_employee", TABLE, ["company_id", "employee_id", "first_seen_at", "id"], schema=WORKFORCE
    )
    op.create_index(
        "ix_employee_devices_reviewed_by_id",
        TABLE,
        ["reviewed_by_id"],
        schema=WORKFORCE,
        postgresql_where=sa.text("reviewed_by_id IS NOT NULL"),
    )
    qualified = f"{WORKFORCE}.{TABLE}"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {qualified} USING ({EXPRESSION}) WITH CHECK ({EXPRESSION})")
    op.execute(f"COMMENT ON COLUMN {qualified}.company_id IS '{COMPANY_COMMENT}'")


def upgrade() -> None:
    seed, english = _seed("catalogs.json"), _seed(f"catalogs.{LOCALE}.json")
    _catalog_rows(seed)
    _texts(seed, english)
    _devices()
    # Tabla particionada: la columna nueva (nula, sin valor por omisión) pasa a todas sus particiones al instante.
    op.add_column("attendance_events", sa.Column("ip_country", sa.String(length=2), nullable=True), schema=ATTENDANCE)
    op.add_column("attendance_events", sa.Column("ip_asn", sa.Integer(), nullable=True), schema=ATTENDANCE)
    op.get_bind().exec_driver_sql((SQL_DIR / "0065_comments.sql").read_text(encoding="utf-8").replace("%", "%%"))


def downgrade() -> None:
    op.drop_column("attendance_events", "ip_asn", schema=ATTENDANCE)
    op.drop_column("attendance_events", "ip_country", schema=ATTENDANCE)
    op.drop_table(TABLE, schema=WORKFORCE)
    bind = op.get_bind()
    for (catalog, code, column), (spanish, english) in PREVIOUS_TEXTS.items():
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
    op.execute(f"DELETE FROM {CATALOG}.translations WHERE catalog = 'risk_signals' AND code IN ({_in(NEW_SIGNALS)})")
    op.execute(
        f"DELETE FROM {CATALOG}.translations WHERE catalog = 'review_reasons' AND code IN ({_in(NEW_REVIEW_REASONS)})"
    )
    op.execute(f"DELETE FROM ops.risk_signal_stats WHERE signal IN ({_in(NEW_SIGNALS)})")
    op.execute(f"DELETE FROM {CATALOG}.risk_signals WHERE code IN ({_in(NEW_SIGNALS)})")
    op.execute(f"DELETE FROM {CATALOG}.review_reasons WHERE code IN ({_in(NEW_REVIEW_REASONS)})")
