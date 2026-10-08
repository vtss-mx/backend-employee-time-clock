"""API pública de verificación facial para los SDK de Android e iOS (decisión del dueño del producto, 2026-10-07)

«Genera un SDK para Android y otro para iOS que se pueda usar con la API pública para los procesos de verificación… lo
que se tiene que pasar es el API key a esos SDK». El contrato vive en `docs/sdk/contrato-verificacion.md` (raíz) y las
rutas en `app/routers/integration_verification.py` (`/integrations/v1/verification/challenge|verify|identify`), con el
MISMO motor y las mismas cerraduras de la aplicación web (`identity_core`).

- `catalog.api_scopes`: permiso nuevo `VERIFICATION` (solo abre esas tres rutas: no lee empleados, asistencia ni
  validadores) con sus textos en los 7 idiomas (es-MX en la tabla; los demás en `catalog.translations`).
- `catalog.verification_methods`: método nuevo `API_FACE`, para que la bitácora (`attendance.verification_logs`)
  distinga lo que vino de la aplicación móvil de la empresa; también en los 7 idiomas.
- `biometrics.face_challenges`: el reto puede ser de una CUENTA (`user_id`, como hasta ahora) o de un DISPOSITIVO de una
  llave de la API (`api_key_id` + `device_hash`, la huella SHA-256 de la llave pública del dispositivo). `user_id` pasa
  a aceptar nulos; la restricción `ck_face_challenges_owner` exige exactamente un dueño. Índice parcial
  `ix_face_challenges_api_device (api_key_id, device_hash) WHERE api_key_id IS NOT NULL`: el reto vigente de un
  dispositivo (lo reemplaza el siguiente) y la FK a la llave (CASCADE: borrar la llave borra sus retos). La tabla es
  pequeña (retos vigentes; el mantenimiento depura los vencidos): DDL normal.
- `attendance.verification_logs.device_hash` (nueva, nula en lo anterior y en la aplicación web) e índice parcial
  `ix_verification_logs_device_created (device_hash, created_at, id) INCLUDE (success, method, reason, company_id)
  WHERE device_hash IS NOT NULL`: el bloqueo por intentos sospechosos de la identificación 1:N POR DISPOSITIVO
  (`attempt_guard.ensure_unlocked`, la misma consulta que el bloqueo del validador). La tabla es GRANDE y particionada:
  la columna se agrega en la padre (sin reescribir: nula por omisión) y el índice se crea SIN bloquear las escrituras
  (`CREATE INDEX ON ONLY` en la padre, `CONCURRENTLY` en cada partición y `ATTACH PARTITION`; las particiones que cree
  después `ops.ensure_partitions` lo heredan solas). Repetible tras una falla (`IF NOT EXISTS`; adjuntar uno ya
  adjunto no hace nada).

Compatible con la versión anterior en marcha (solo agrega; `user_id` nulo solo lo escribe la versión nueva).
`downgrade` quita los retos de la API, lo nuevo de las dos tablas, el permiso (y su asignación a las llaves) y el
método; si ya hay intentos `API_FACE` en la bitácora, el método se queda (la FK de la bitácora lo exige).

Revision ID: 0084
Revises: 0083
Create Date: 2026-10-07 21:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.sql_safety import sql_identifier

revision: str = "0084"
down_revision: str | None = "0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
BIOMETRICS = "biometrics"
TENANCY = "tenancy"
ATTENDANCE = "attendance"
SCOPE = "VERIFICATION"
METHOD = "API_FACE"
#: Las filas nuevas del catálogo: (catálogo, código, llave de la fila).
NEW_ROWS = (("api_scopes", SCOPE), ("verification_methods", METHOD))
FK_API_KEY = "fk_face_challenges_api_key_id_company_api_keys"
OWNER_CHECK = "ck_face_challenges_owner"
CHALLENGE_INDEX = "ix_face_challenges_api_device"
LOG_INDEX = "ix_verification_logs_device_created"
#: El índice del bloqueo por dispositivo en la PADRE (sin construir: válido cuando cada partición adjunta el suyo).
LOG_INDEX_PARENT = (
    "CREATE INDEX IF NOT EXISTS ix_verification_logs_device_created ON ONLY attendance.verification_logs "
    "(device_hash, created_at, id) INCLUDE (success, method, reason, company_id) WHERE device_hash IS NOT NULL"
)
LOG_INDEX_DROP = "DROP INDEX IF EXISTS attendance.ix_verification_logs_device_created"
#: Las particiones de la bitácora (las crea y borra `ops.ensure_partitions`): sus nombres salen del catálogo de la base.
PARTITIONS = sa.text(
    "SELECT c.relname FROM pg_inherits i "
    "JOIN pg_class c ON c.oid = i.inhrelid "
    "JOIN pg_class p ON p.oid = i.inhparent "
    "JOIN pg_namespace n ON n.oid = p.relnamespace "
    "WHERE n.nspname = 'attendance' AND p.relname = 'verification_logs' ORDER BY c.relname"
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


def _catalog_rows() -> None:
    """Las filas del seed vigente (idempotente: una base nueva ya las cargó con el seed)."""
    seed = _seed("catalogs.json")
    for catalog, code in NEW_ROWS:
        for row in (r for r in seed[catalog] if r["code"] == code):
            table = sa.table(catalog, *(sa.column(key) for key in row), schema=CATALOG)
            op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=["code"]))


def _translations() -> None:
    """Los textos de las filas nuevas en cada idioma con archivo de traducciones (`catalogs.<idioma>.json`)."""
    rows = []
    for path in sorted(SEED_DIR.glob("catalogs.*.json")):
        locale = path.name.removeprefix("catalogs.").removesuffix(".json")
        texts = json.loads(path.read_text(encoding="utf-8"))
        for catalog, code in NEW_ROWS:
            rows += [
                {"catalog": catalog, "code": code, "locale": locale, "field": field, "text": text}
                for field, text in texts.get(catalog, {}).get(code, {}).items()
            ]
    if not rows:
        return
    statement = postgresql.insert(_TRANSLATIONS).values(rows)
    op.execute(
        statement.on_conflict_do_update(
            index_elements=["catalog", "code", "locale", "field"], set_={"text": statement.excluded.text}
        )
    )


def _challenge_owner() -> None:
    op.alter_column("face_challenges", "user_id", existing_type=sa.Integer(), nullable=True, schema=BIOMETRICS)
    op.add_column("face_challenges", sa.Column("api_key_id", sa.Integer(), nullable=True), schema=BIOMETRICS)
    op.add_column("face_challenges", sa.Column("device_hash", sa.String(length=64), nullable=True), schema=BIOMETRICS)
    op.create_foreign_key(
        op.f(FK_API_KEY),
        "face_challenges",
        "company_api_keys",
        ["api_key_id"],
        ["id"],
        source_schema=BIOMETRICS,
        referent_schema=TENANCY,
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        op.f(OWNER_CHECK),
        "face_challenges",
        "(user_id IS NULL) <> (api_key_id IS NULL) AND (api_key_id IS NULL) = (device_hash IS NULL)",
        schema=BIOMETRICS,
    )
    op.create_index(
        op.f(CHALLENGE_INDEX),
        "face_challenges",
        ["api_key_id", "device_hash"],
        schema=BIOMETRICS,
        postgresql_where=sa.text("api_key_id IS NOT NULL"),
    )


def _log_device_index() -> None:
    """El índice del bloqueo por dispositivo sin bloquear las escrituras de la bitácora (tabla grande, particionada)."""
    op.execute(LOG_INDEX_PARENT)
    partitions = [row[0] for row in op.get_bind().execute(PARTITIONS)]
    # CONCURRENTLY no puede ir dentro de una transacción: cada sentencia se confirma sola (repetible tras una falla).
    with op.get_context().autocommit_block():
        for partition in partitions:
            table = sql_identifier(f"{ATTENDANCE}.{partition}")
            index = sql_identifier(f"{partition}_device_idx")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index} ON {table} "
                "(device_hash, created_at, id) INCLUDE (success, method, reason, company_id) "
                "WHERE device_hash IS NOT NULL"
            )
            op.execute(
                f"ALTER INDEX attendance.ix_verification_logs_device_created ATTACH PARTITION "
                f"{sql_identifier(f'{ATTENDANCE}.{partition}_device_idx')}"
            )


def upgrade() -> None:
    _catalog_rows()
    _translations()
    _challenge_owner()
    op.add_column("verification_logs", sa.Column("device_hash", sa.String(length=64), nullable=True), schema=ATTENDANCE)
    _log_device_index()


def downgrade() -> None:
    # Borrar el índice de la padre borra el de cada partición (uno particionado no admite CONCURRENTLY).
    op.execute(LOG_INDEX_DROP)
    op.drop_column("verification_logs", "device_hash", schema=ATTENDANCE)
    challenges = sa.table("face_challenges", sa.column("api_key_id"), schema=BIOMETRICS)
    op.execute(sa.delete(challenges).where(challenges.c.api_key_id.is_not(None)))
    op.drop_index(op.f(CHALLENGE_INDEX), table_name="face_challenges", schema=BIOMETRICS)
    op.drop_constraint(op.f(OWNER_CHECK), "face_challenges", schema=BIOMETRICS, type_="check")
    op.drop_constraint(op.f(FK_API_KEY), "face_challenges", schema=BIOMETRICS, type_="foreignkey")
    op.drop_column("face_challenges", "device_hash", schema=BIOMETRICS)
    op.drop_column("face_challenges", "api_key_id", schema=BIOMETRICS)
    op.alter_column("face_challenges", "user_id", existing_type=sa.Integer(), nullable=False, schema=BIOMETRICS)
    for catalog, code in NEW_ROWS:
        op.execute(sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.catalog == catalog, _TRANSLATIONS.c.code == code))
    scopes = sa.table("company_api_key_scopes", sa.column("scope"), schema=TENANCY)
    op.execute(sa.delete(scopes).where(scopes.c.scope == SCOPE))
    api_scopes = sa.table("api_scopes", sa.column("code"), schema=CATALOG)
    op.execute(sa.delete(api_scopes).where(api_scopes.c.code == SCOPE))
    # El método se queda si la bitácora ya lo usa (su FK lo exige): solo sale si nadie lo nombra.
    logs = sa.table("verification_logs", sa.column("method"), schema=ATTENDANCE)
    methods = sa.table("verification_methods", sa.column("code"), schema=CATALOG)
    used = sa.exists().where(logs.c.method == METHOD)
    op.execute(sa.delete(methods).where(methods.c.code == METHOD, ~used))
