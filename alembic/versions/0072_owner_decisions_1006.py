"""Decisiones del dueño del producto del 2026-10-06: índices de la depuración de «Eliminados», el desglose de los
días-persona del cobro, «administrador» en lugar del código «ADMIN» en los textos y los niveles de la política con la
fase 2b del antifraude

1. **Índices de la depuración** (`backend-employee-time-clock/AGENTS.md` §3.1.8; README "Índices de base de datos",
   medidos con `perf/db/run.sh` antes y después): la evidencia de un empleado que se elimina
   (`ops.fraud_evidence (company_id, employee_id)`, parcial `employee_id IS NOT NULL`: la sacan del bucket y la borran
   `release_employee_images` y `erase_employee`) y las llaves foráneas `ON DELETE SET NULL` de `ops.fraud_cases` hacia
   las cuentas (`actor_id`, `decided_by_id`, parciales `IS NOT NULL`: la depuración de las cuentas de «Eliminados» las
   revisa por cada cuenta). Sin ellos, cada eliminación de un empleado y cada lote de cuentas recorría esas tablas. Se
   construyen `CONCURRENTLY` fuera de la transacción (§3.1.13: no frenan lecturas ni escrituras), cada uno con su
   `DROP ... IF EXISTS` antes (repetible tras una falla a la mitad: un índice a medias queda inválido).
2. **Desglose de los días-persona del cobro** (las unidades del cobro por empleado activo incluyen a los validadores
   desde la `0067`; la app ya no dice «días-empleado» sino «días-persona (N de empleados, M de validadores)»):
   `billing.charges.validator_units` y `billing.charge_lines.validator_units`, cuántos de esos días fueron de
   validadores. Nulas: un cargo emitido antes no tiene el dato (se muestra sin desglose) y el monto fijo no lo usa;
   las réplicas anteriores siguen escribiendo sin ellas (despliegue gradual). El dinero no cambia. CHECK
   `0 <= validator_units <= units` `NOT VALID` y después `VALIDATE` (no frena la tabla mientras revisa).
3. **Textos de los catálogos** (§11.3, español en la columna e inglés en `catalog.translations`):
   - el código del rol como palabra («otro ADMIN», «Un ADMIN…», "another ADMIN") pasa a «administrador» / "admin" en
     las descripciones de `risk_actions`, `policy_change_statuses`, `fraud_case_statuses` y `fraud_case_event_kinds`
     (los códigos no cambian);
   - las descripciones de los niveles (`policy_presets`): Máximo exige el destello dictado y la ráfaga (fase 2a) y la
     firma y la ubicación de los validadores y el código de sitio (fase 2b); Alto y Estándar los dejan en «Solo medir».
   Cada texto lleva aquí su versión anterior y la nueva (no se leen del seed), así que `downgrade` deja exactamente lo
   que había. Sin SQL armada: los textos y códigos viajan como parámetros (regla 21 de la raíz) y el DDL son constantes.

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-06 18:00:00
"""

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BILLING = "billing"
CATALOG = "catalog"
LOCALE = "en-US"
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

#: Índices nuevos: (crear, borrar). Cada sentencia es una constante (sin datos ni nombres armados).
INDEXES: tuple[tuple[str, str], ...] = (
    (
        "CREATE INDEX CONCURRENTLY ix_fraud_evidence_employee ON ops.fraud_evidence (company_id, employee_id) "
        "WHERE employee_id IS NOT NULL",
        "DROP INDEX CONCURRENTLY IF EXISTS ops.ix_fraud_evidence_employee",
    ),
    (
        "CREATE INDEX CONCURRENTLY ix_fraud_cases_actor_id ON ops.fraud_cases (actor_id) WHERE actor_id IS NOT NULL",
        "DROP INDEX CONCURRENTLY IF EXISTS ops.ix_fraud_cases_actor_id",
    ),
    (
        "CREATE INDEX CONCURRENTLY ix_fraud_cases_decided_by_id ON ops.fraud_cases (decided_by_id) "
        "WHERE decided_by_id IS NOT NULL",
        "DROP INDEX CONCURRENTLY IF EXISTS ops.ix_fraud_cases_decided_by_id",
    ),
)
#: El desglose de los días-persona: (tabla, restricción, agregarla sin revisar, revisarla).
CHECKS: tuple[tuple[str, str, str, str], ...] = (
    (
        "charges",
        "ck_charges_validator_units",
        "ALTER TABLE billing.charges ADD CONSTRAINT ck_charges_validator_units "
        "CHECK (validator_units >= 0 AND validator_units <= units) NOT VALID",
        "ALTER TABLE billing.charges VALIDATE CONSTRAINT ck_charges_validator_units",
    ),
    (
        "charge_lines",
        "ck_charge_lines_validator_units",
        "ALTER TABLE billing.charge_lines ADD CONSTRAINT ck_charge_lines_validator_units "
        "CHECK (validator_units >= 0 AND validator_units <= units) NOT VALID",
        "ALTER TABLE billing.charge_lines VALIDATE CONSTRAINT ck_charge_lines_validator_units",
    ),
)

#: (catálogo, código, columna) → ((español anterior, español nuevo), (inglés anterior, inglés nuevo)).
TEXTS: dict[tuple[str, str, str], tuple[tuple[str, str], tuple[str, str]]] = {
    ("risk_actions", "ALERT", "description"): (
        ("El intento sigue, pero se abre un caso para que el ADMIN lo revise.", "El intento sigue, pero se abre un caso para que el administrador lo revise."),
        ("The attempt continues, but a case is opened for the ADMIN to review.", "The attempt continues, but a case is opened for the admin to review."),
    ),
    ("policy_change_statuses", "PENDING", "description"): (
        ("Relaja la seguridad: espera la aprobación de otro ADMIN.", "Relaja la seguridad: espera la aprobación de otro administrador."),
        ("It lowers security: it's waiting for another ADMIN's approval.", "It lowers security: it's waiting for another admin's approval."),
    ),
    ("policy_change_statuses", "REJECTED", "description"): (
        ("Otro ADMIN lo rechazó: no se aplicó.", "Otro administrador lo rechazó: no se aplicó."),
        ("Another ADMIN rejected it: it wasn't applied.", "Another admin rejected it: it wasn't applied."),
    ),
    ("fraud_case_statuses", "IN_REVIEW", "description"): (
        ("Un ADMIN lo está revisando.", "Un administrador lo está revisando."),
        ("An ADMIN is reviewing it.", "An admin is reviewing it."),
    ),
    ("fraud_case_event_kinds", "STATUS_CHANGED", "description"): (
        ("Un ADMIN cambió el estado del caso.", "Un administrador cambió el estado del caso."),
        ("An ADMIN changed the case status.", "An admin changed the case status."),
    ),
    ("fraud_case_event_kinds", "NOTE", "description"): (
        ("Un ADMIN agregó una nota.", "Un administrador agregó una nota."),
        ("An ADMIN added a note.", "An admin added a note."),
    ),
    ("fraud_case_event_kinds", "EVIDENCE_VIEWED", "description"): (
        ("Un ADMIN vio los fotogramas de evidencia.", "Un administrador vio los fotogramas de evidencia."),
        ("An ADMIN viewed the evidence frames.", "An admin viewed the evidence frames."),
    ),
    ("policy_presets", "STANDARD", "description"): (
        ("Para la mayoría: dos movimientos en 60 s, destello en Solo medir hasta calibrarlo, anti-spoofing Estándar, dispositivo del empleado en Solo medir y cortes de riesgo 30/60/80.", "Para la mayoría: dos movimientos en 60 s, anti-spoofing Estándar y cortes de riesgo 30/60/80. El destello (hasta calibrarlo), el dispositivo del empleado, el protocolo de captura y la presencia de los validadores, en Solo medir."),
        ("For most companies: two movements in 60 s, flash in Measure only until calibrated, Standard anti-spoofing, the employee's device in Measure only, and risk cutoffs 30/60/80.", "For most companies: two movements in 60 s, Standard anti-spoofing, and risk cutoffs 30/60/80. The flash (until calibrated), the employee's device, the capture protocol, and validator presence stay in Measure only."),
    ),
    ("policy_presets", "HIGH", "description"): (
        ("Más exigente: tres movimientos en 45 s, destello obligatorio, anti-spoofing Alto, reenvío perceptual obligatorio, un paso más en un dispositivo nuevo y cortes 25/50/75.", "Más exigente: tres movimientos en 45 s, destello obligatorio, anti-spoofing Alto, reenvío perceptual obligatorio, un paso más en un dispositivo nuevo y cortes 25/50/75. El protocolo de captura y la presencia de los validadores, en Solo medir."),
        ("Stricter: three movements in 45 s, required flash, High anti-spoofing, required perceptual resubmission check, one more step on a new device, and cutoffs 25/50/75.", "Stricter: three movements in 45 s, required flash, High anti-spoofing, required perceptual resubmission check, one more step on a new device, and cutoffs 25/50/75. The capture protocol and validator presence stay in Measure only."),
    ),
    ("policy_presets", "MAXIMUM", "description"): (
        ("Para sitios con fraude confirmado: tres movimientos en 30 s, anti-spoofing Máximo, reglas duras obligatorias, la empresa aprueba cada dispositivo nuevo, sin destello dictado o sin ráfaga se pide un paso más y el riesgo alto se niega. Más reintentos con poca luz.", "Para fraude confirmado: tres movimientos en 30 s, anti-spoofing Máximo, reglas duras obligatorias, la empresa aprueba cada dispositivo y el riesgo alto se niega. Exige destello dictado y ráfaga (si faltan, un paso más), firma y ubicación de validadores y código de sitio. Más reintentos con poca luz."),
        ("For sites with confirmed fraud: three movements in 30 s, Maximum anti-spoofing, required hard rules, the company approves each new device, a missing paced flash or burst asks for one more step, and high risk is denied. More retries in low light.", "For confirmed fraud: three movements in 30 s, Maximum anti-spoofing, required hard rules, the company approves each device, and high risk is denied. Requires the paced flash and burst (if missing, one more step), validator signing and location, and site codes. More retries in low light."),
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


def _texts(new: bool) -> None:
    """Cada texto en español (columna del catálogo) y en inglés (`catalog.translations`); una sentencia por texto."""
    pick = 1 if new else 0
    for (catalog, code, column), (spanish, english) in TEXTS.items():
        table = sa.table(catalog, sa.column("code"), sa.column(column), schema=CATALOG)
        op.execute(sa.update(table).where(table.c.code == code).values({column: spanish[pick]}))
        op.execute(
            sa.update(_TRANSLATIONS)
            .where(
                _TRANSLATIONS.c.catalog == catalog,
                _TRANSLATIONS.c.code == code,
                _TRANSLATIONS.c.locale == LOCALE,
                _TRANSLATIONS.c.field == column,
            )
            .values(text=english[pick])
        )


def upgrade() -> None:
    for table, _, add, _ in CHECKS:
        op.add_column(table, sa.Column("validator_units", sa.Integer(), nullable=True), schema=BILLING)
        op.execute(add)
    _texts(new=True)
    op.get_bind().exec_driver_sql((SQL_DIR / "0072_comments.sql").read_text(encoding="utf-8").replace("%", "%%"))
    # CONCURRENTLY y VALIDATE no pueden ir dentro de una transacción: cada sentencia se confirma sola.
    with op.get_context().autocommit_block():
        for _, _, _, validate in CHECKS:
            op.execute(validate)
        for create, drop in INDEXES:
            op.execute(drop)
            op.execute(create)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for _, drop in INDEXES:
            op.execute(drop)
    _texts(new=False)
    for table, name, _, _ in CHECKS:
        op.drop_constraint(op.f(name), table, schema=BILLING, type_="check")
        op.drop_column(table, "validator_units", schema=BILLING)
