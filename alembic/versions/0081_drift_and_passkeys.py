"""Antifraude fase 3: monitoreo de deriva de las señales y llaves de acceso (WebAuthn / passkeys)

Decisión del dueño del producto (2026-10-06): hacer ahora la fase 3 del documento de I+D (`docs/rd/antifraude-identidad.md`
§3.5 y P6) y dejar el clasificador propio (D5) pendiente de datos reales.

**Deriva de las señales** (el mantenimiento, al cerrar cada ventana de `DRIFT_WINDOW_DAYS`):
- `ops.face_attempt_metrics.platform` (nueva, nula en lo anterior): la plataforma gruesa del navegador del intento
  (IOS_SAFARI, ANDROID_CHROME, DESKTOP, OTHER; `app/core/devices.py`). Una categoría, nunca el User-Agent ni la persona:
  agrupa la deriva sin guardar nada nuevo de nadie. La tabla está particionada: la columna se agrega en la padre.
- `ops.signal_drift` (plataforma, sin `company_id`): una fila por ventana × señal × plataforma con la mediana, la cola
  vigilada (p10 de un mínimo, p90 de un máximo) y el PSI frente a la ventana anterior, y su estado. Único
  `(week_start, signal, platform)`: el cálculo reemplaza la ventana y el listado del ADMIN filtra por ella.
- `ops.company_fraud_weekly` (de EMPRESA, con seguridad por fila): por empresa y ventana, intentos, casos de fraude,
  revisiones decididas, aprobadas y aprobadas "sin mirar" (fraude interno). Único `(company_id, week_start)` (también
  la FK) e índice `(week_start, id)` para el listado por ventana y la depuración.
- `ops.engine_versions`: la bitácora del motor (motor de riesgo, modelos faciales, API, aplicación web): una ventana en
  la que cambió el motor o los modelos no se compara con la anterior.
- `attendance.work_sessions`: índice parcial `ix_work_sessions_reviewed (reviewed_at, company_id) WHERE reviewed_at IS
  NOT NULL` para leer las revisiones decididas de una ventana sin recorrer las jornadas (tabla grande:
  `CONCURRENTLY`, fuera de la transacción, repetible con `IF NOT EXISTS`).
- Pantalla `ADMIN_DRIFT` («Deriva de señales», módulo Operación) del ADMIN con sus traducciones.

**Llaves de acceso** (`auth.passkeys`, de la PERSONA como la foto de perfil: sin `company_id`): la llave PÚBLICA de
cada credencial, su contador de firmas (detección de copias), transportes, nombre y uso; revocar es un borrado real.
`auth.passkey_challenges`: la huella de cada reto sellado ya aceptado (un reto vale una sola vez), depurada al vencer.

Compatible con la versión anterior en marcha (solo agrega). Los permisos de la API sobre las tablas nuevas los pone
`python -m app.cli db roles` en el mismo despliegue (servicio `migrate`). `downgrade` quita todo lo nuevo (los retos
sellados vencen solos; las llaves registradas se pierden: la persona vuelve a entrar con su contraseña).

Revision ID: 0081
Revises: 0080
Create Date: 2026-10-07 09:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
OPS = "ops"
AUTH = "auth"
SCREEN = "ADMIN_DRIFT"
#: La seguridad por fila de la tabla de empresa nueva (la misma SQL que `row_security.policy_ddl`), en constantes.
ROW_SECURITY = (
    "ALTER TABLE ops.company_fraud_weekly ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE ops.company_fraud_weekly FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON ops.company_fraud_weekly "
    "USING (company_id = NULLIF(current_setting('app.company_id', true), '')::integer) "
    "WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), '')::integer)",
    "COMMENT ON COLUMN ops.company_fraud_weekly.company_id IS 'Empresa dueña de la fila. Seguridad por fila "
    "(política tenant_isolation): solo se ve y se escribe en una transacción con app.company_id igual (o con el rol de "
    "la plataforma).'",
)
#: El índice de las revisiones decididas sobre una tabla grande: fuera de la transacción y repetible.
REVIEWED_INDEX_CREATE = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_work_sessions_reviewed ON attendance.work_sessions "
    "(reviewed_at, company_id) WHERE reviewed_at IS NOT NULL"
)
REVIEWED_INDEX_DROP = "DROP INDEX CONCURRENTLY IF EXISTS attendance.ix_work_sessions_reviewed"
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


def _screen(seed: dict) -> None:
    for row in (r for r in seed["screens"] if r["code"] == SCREEN):
        _insert_missing("screens", row, ["code"])
    for grant in (g for g in seed["role_screens"] if g["screen_code"] == SCREEN):
        _insert_missing("role_screens", grant, ["role_code", "screen_code"])
    for link in (m for m in seed["menu_module_screens"] if m["screen_code"] == SCREEN):
        _insert_missing("menu_module_screens", link, ["screen_code"])


def _translations() -> None:
    """La pantalla en cada idioma con archivo de traducciones (`catalogs.<idioma>.json`)."""
    rows = []
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


def _drift_tables() -> None:
    op.create_table(
        "signal_drift",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("signal", sa.String(length=40), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("baseline_samples", sa.Integer(), nullable=False),
        sa.Column("median", sa.Float(), nullable=True),
        sa.Column("baseline_median", sa.Float(), nullable=True),
        sa.Column("tail", sa.Float(), nullable=True),
        sa.Column("baseline_tail", sa.Float(), nullable=True),
        sa.Column("tail_percentile", sa.SmallInteger(), nullable=False),
        sa.Column("tail_change", sa.Float(), nullable=True),
        sa.Column("psi", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_drift")),
        sa.UniqueConstraint("week_start", "signal", "platform", name="uq_signal_drift_window"),
        schema=OPS,
    )
    op.create_table(
        "company_fraud_weekly",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("company_name", sa.String(length=150), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("fraud_cases", sa.Integer(), nullable=False),
        sa.Column("case_rate", sa.Float(), nullable=True),
        sa.Column("reviews", sa.Integer(), nullable=False),
        sa.Column("approved", sa.Integer(), nullable=False),
        sa.Column("quick_approvals", sa.Integer(), nullable=False),
        sa.Column("quick_rate", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["tenancy.companies.id"],
            name=op.f("fk_company_fraud_weekly_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_fraud_weekly")),
        sa.UniqueConstraint("company_id", "week_start", name="uq_company_fraud_weekly_window"),
        schema=OPS,
    )
    op.create_index("ix_company_fraud_weekly_week", "company_fraud_weekly", ["week_start", "id"], schema=OPS)
    for statement in ROW_SECURITY:
        op.execute(statement)
    op.create_table(
        "engine_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("component", sa.String(length=30), nullable=False),
        sa.Column("version", sa.String(length=120), nullable=False),
        sa.Column("noted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_engine_versions")),
        schema=OPS,
    )
    op.create_index("ix_engine_versions_component", "engine_versions", ["component", "noted_at", "id"], schema=OPS)
    op.create_index("ix_engine_versions_noted", "engine_versions", ["noted_at", "id"], schema=OPS)


def _passkey_tables() -> None:
    op.create_table(
        "passkeys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.String(length=1400), nullable=False),
        sa.Column("public_key", sa.String(length=2048), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("transports", sa.String(length=100), nullable=True),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("aaguid", sa.String(length=36), nullable=True),
        sa.Column("backup_eligible", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("backed_up", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["auth.users.id"], name=op.f("fk_passkeys_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_passkeys")),
        schema=AUTH,
    )
    op.create_index("uq_passkeys_credential", "passkeys", ["credential_id"], unique=True, schema=AUTH)
    op.create_index("ix_passkeys_user", "passkeys", ["user_id", "id"], schema=AUTH)
    op.create_table(
        "passkey_challenges",
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("digest", name=op.f("pk_passkey_challenges")),
        schema=AUTH,
    )
    op.create_index(op.f("ix_passkey_challenges_expires_at"), "passkey_challenges", ["expires_at"], schema=AUTH)


def upgrade() -> None:
    op.add_column("face_attempt_metrics", sa.Column("platform", sa.String(length=20), nullable=True), schema=OPS)
    _drift_tables()
    _passkey_tables()
    _screen(_seed("catalogs.json"))
    _translations()
    # CONCURRENTLY no puede ir dentro de una transacción: la sentencia se confirma sola (repetible tras una falla).
    with op.get_context().autocommit_block():
        op.execute(REVIEWED_INDEX_CREATE)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(REVIEWED_INDEX_DROP)
    op.execute(sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.catalog == "screens", _TRANSLATIONS.c.code == SCREEN))
    for table, column in (("menu_module_screens", "screen_code"), ("role_screens", "screen_code"), ("screens", "code")):
        rows = sa.table(table, sa.column(column), schema=CATALOG)
        op.execute(sa.delete(rows).where(rows.c[column] == SCREEN))
    op.drop_table("passkey_challenges", schema=AUTH)
    op.drop_table("passkeys", schema=AUTH)
    op.drop_table("engine_versions", schema=OPS)
    op.drop_table("company_fraud_weekly", schema=OPS)
    op.drop_table("signal_drift", schema=OPS)
    op.drop_column("face_attempt_metrics", "platform", schema=OPS)
