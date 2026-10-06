"""Observabilidad de rendimiento: observadores del backend y del navegador, alertas de peticiones lentas y la
pantalla "Rendimiento" del ADMIN

Pedido del dueño del producto: "observadores globales del frontend y del backend... la aplicación web debe tener un
módulo para ver el comportamiento del rendimiento de las funciones y las APIs", más la regla 18 del `AGENTS.md` raíz
(toda petición de más de 1 s alerta al ADMIN). Decisiones (detalle en el README, "Observabilidad de rendimiento"):

- **Cero trabajo de BD en la petición**: los observadores suman en memoria por minuto (`app/core/perf_meter.py`) y
  un hilo guarda en lotes con UPSERT que SUMAN (patrón de `usage_meter`). Los percentiles salen de un histograma de
  cubetas FIJAS (`app/core/histogram.py`): se suma entre réplicas y entre minutos, un percentil no.
- **Datos de la plataforma, no de una empresa**: ninguna tabla nueva lleva `company_id` (se mide cada ruta, función
  y pantalla de toda la plataforma), así que no llevan seguridad por fila: solo las escriben el guardado en lotes y
  el mantenimiento (rol de la plataforma) y solo las lee el ADMIN. Ninguna guarda datos de una persona.
- `ops.perf_minutes` (tipo, minuto, nombre + contadores, tiempos, BD, bytes y 18 cubetas `h_*`): crece con cada
  minuto → PARTICIONADA por mes en `minute` desde su creación (partición `_default` y los meses que vienen; la
  función `ops.ensure_partitions` la acepta desde `alembic/sql/0063_partitions.sql`, con espacio libre en cada
  partición porque cada lote suma a las filas del minuto en curso). Retención `PERF_MINUTE_RETENTION_DAYS`.
- `ops.perf_hours` y `ops.perf_days`: resúmenes que arma el mantenimiento para los periodos largos; su retención las
  acota (no se particionan), `fillfactor` 80 (cada vuelta vuelve a sumar las horas y el día recientes).
- `ops.slow_request_alerts` (regla 18): UNA fila por ruta con su contador, tiempos (último, máximo, total), último
  traceId, estado HTTP, una muestra del contexto sin secretos ni cuerpos y su seguimiento (catálogo nuevo
  `slow_alert_statuses`: abierta, en atención, resuelta; una resuelta que vuelve a ocurrir se reabre sola).
- `ops.top_statements` (`alembic/sql/0063_performance.sql`): SECURITY DEFINER del dueño que entrega al rol de la
  plataforma el texto NORMALIZADO de `pg_stat_statements` de esta base (sin parámetros).
- Menú: pantalla `ADMIN_PERFORMANCE` ("Rendimiento", módulo Operación, contador `OPEN_SLOW_ALERTS`) del ADMIN; el
  módulo Operación menciona el rendimiento en su descripción.

Índices: la llave primaria `(kind, <tiempo>, name)` de cada grano sirve a todas sus consultas (tipo por igualdad,
rango de tiempo y el nombre al final) y a su depuración; las alertas, su ruta única (el UPSERT del guardado) y la
bandeja por seguimiento y por fecha. Todas las tablas son nuevas: se crean sin `CONCURRENTLY`.

`downgrade` quita lo nuevo y deja `ops.ensure_partitions` como estaba (su archivo SQL anterior, sin la tabla nueva).

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-08 10:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
SEED_FILE = Path(__file__).resolve().parents[1] / "seed" / "catalogs.json"
OPS = "ops"
CATALOG = "catalog"
SCREEN = "ADMIN_PERFORMANCE"
MODULE = "OPERATIONS"
STATUSES = "slow_alert_statuses"
#: La descripción del módulo Operación antes de esta migración (para el `downgrade`).
PREVIOUS_MODULE_DESCRIPTION = "Salud del sistema: errores y estado del servidor."
#: El comentario del esquema `ops` antes de esta migración (`0056_comments.sql`).
PREVIOUS_OPS_COMMENT = (
    "Operación de la plataforma: errores, métricas faciales, consumo, almacenamiento y tareas del mantenimiento."
)
#: Meses que se dejan creados por adelantado (después lo hace el mantenimiento: PARTITION_MONTHS_AHEAD).
MONTHS_AHEAD = 3
#: Cubetas del histograma (`app/core/histogram.py`): límite superior en ms; `h_inf` recibe lo que pasa de 10 s.
BOUNDS_MS = (1, 2, 5, 10, 20, 35, 50, 75, 100, 150, 250, 400, 600, 1000, 2000, 5000, 10000)


def _run_sql(name: str) -> None:
    """SQL de varias sentencias tal cual (psycopg toma `%` como marcador de parámetro: se escribe doble)."""
    op.get_bind().exec_driver_sql((SQL_DIR / name).read_text(encoding="utf-8").replace("%", "%%"))


def _previous_partitions_sql() -> str:
    """El archivo de `ops.ensure_partitions` anterior a esta migración (el último `<revisión>_partitions.sql` antes de
    `0063`): el `downgrade` la deja exactamente como estaba."""
    previous = [path.name for path in sorted(SQL_DIR.glob("*_partitions.sql")) if path.name < "0063"]
    return previous[-1]


def _insert_missing(table_name: str, row: dict, keys: list[str]) -> None:
    """Inserta la fila del seed si no está (idempotente)."""
    table = sa.table(table_name, *(sa.column(key) for key in row), schema=CATALOG)
    op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=keys))


def _counters() -> list[sa.Column]:
    """Los contadores de un grano (los mismos de `PerfCounters`)."""
    zero = sa.text("0")
    counts = ("count", "errors", "client_errors")
    columns = [sa.Column(name, sa.BigInteger(), server_default=zero, nullable=False) for name in counts]
    columns += [sa.Column(name, sa.Float(), server_default=zero, nullable=False) for name in ("total_ms", "max_ms")]
    columns.append(sa.Column("db_ms", sa.Float(), server_default=zero, nullable=False))
    columns += [
        sa.Column(name, sa.BigInteger(), server_default=zero, nullable=False)
        for name in ("db_queries", "bytes_in", "bytes_out")
    ]
    buckets = [f"h_{bound}" for bound in BOUNDS_MS] + ["h_inf"]
    columns += [sa.Column(name, sa.BigInteger(), server_default=zero, nullable=False) for name in buckets]
    return columns


def _grain(name: str, time_column: sa.Column, **options: object) -> None:
    op.create_table(
        name,
        sa.Column("kind", sa.String(length=16), nullable=False),
        time_column,
        sa.Column("name", sa.String(length=160), nullable=False),
        *_counters(),
        sa.PrimaryKeyConstraint("kind", time_column.name, "name", name=op.f(f"pk_{name}")),
        schema=OPS,
        **options,
    )


def _catalog(seed: dict) -> None:
    table = op.create_table(
        STATUSES,
        sa.Column("tone", sa.String(length=20), server_default="muted", nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f(f"pk_{STATUSES}")),
        schema=CATALOG,
    )
    op.bulk_insert(table, seed[STATUSES])


def _menu(seed: dict) -> None:
    for row in (r for r in seed["screens"] if r["code"] == SCREEN):
        _insert_missing("screens", row, ["code"])
    for grant in (g for g in seed["role_screens"] if g["screen_code"] == SCREEN):
        _insert_missing("role_screens", grant, ["role_code", "screen_code"])
    for link in (m for m in seed["menu_module_screens"] if m["screen_code"] == SCREEN):
        _insert_missing("menu_module_screens", link, ["screen_code"])
    module = next(m for m in seed["menu_modules"] if m["code"] == MODULE)
    _module_description(module["description"])


def _module_description(description: str) -> None:
    modules = sa.table("menu_modules", sa.column("code"), sa.column("description"), schema=CATALOG)
    op.execute(sa.update(modules).where(modules.c.code == MODULE).values(description=description))


def _alerts() -> None:
    op.create_table(
        "slow_request_alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("route", sa.String(length=160), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="OPEN", nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("total_ms", sa.Float(), nullable=False),
        sa.Column("last_ms", sa.Float(), nullable=False),
        sa.Column("max_ms", sa.Float(), nullable=False),
        sa.Column("threshold_ms", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_trace_id", sa.String(length=64), nullable=True),
        sa.Column("last_status", sa.SmallInteger(), nullable=True),
        sa.Column("sample", sa.Text(), nullable=True),
        sa.Column("reopened", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_changed_by", sa.String(length=254), nullable=True),
        sa.CheckConstraint("count > 0", name=op.f("ck_slow_request_alerts_count_positive")),
        sa.ForeignKeyConstraint(
            ["status"], [f"{CATALOG}.{STATUSES}.code"], name=op.f("fk_slow_request_alerts_status_slow_alert_statuses")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slow_request_alerts")),
        schema=OPS,
        postgresql_with={"fillfactor": 80},
    )
    op.create_index("uq_slow_request_alerts_route", "slow_request_alerts", ["route"], unique=True, schema=OPS)
    op.create_index(
        "ix_slow_request_alerts_status_seen", "slow_request_alerts", ["status", "last_seen_at", "id"], schema=OPS
    )
    op.create_index("ix_slow_request_alerts_seen", "slow_request_alerts", ["last_seen_at", "id"], schema=OPS)


def upgrade() -> None:
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    _catalog(seed)
    _menu(seed)
    _grain(
        "perf_minutes",
        sa.Column("minute", sa.DateTime(timezone=True), nullable=False),
        postgresql_partition_by="RANGE (minute)",
    )
    op.execute(f"CREATE TABLE {OPS}.perf_minutes_default PARTITION OF {OPS}.perf_minutes DEFAULT")
    hot = {"fillfactor": 80}
    _grain("perf_hours", sa.Column("hour", sa.DateTime(timezone=True), nullable=False), postgresql_with=hot)
    _grain("perf_days", sa.Column("day", sa.Date(), nullable=False), postgresql_with=hot)
    _alerts()
    _run_sql("0063_partitions.sql")
    op.get_bind().execute(
        sa.text("SELECT ops.ensure_partitions(:table, CAST(now() AS date), :ahead, NULL)"),
        {"table": f"{OPS}.perf_minutes", "ahead": MONTHS_AHEAD},
    )
    _run_sql("0063_performance.sql")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ops.top_statements(text, integer, integer)")
    op.drop_table("slow_request_alerts", schema=OPS)
    op.drop_table("perf_days", schema=OPS)
    op.drop_table("perf_hours", schema=OPS)
    op.drop_table("perf_minutes", schema=OPS)  # con sus particiones
    # La función de particiones vuelve a su versión anterior (la del archivo `*_partitions.sql` previo a este).
    _run_sql(_previous_partitions_sql())
    op.execute(f"COMMENT ON SCHEMA {OPS} IS '{PREVIOUS_OPS_COMMENT}'")
    for table, column in (("menu_module_screens", "screen_code"), ("role_screens", "screen_code"), ("screens", "code")):
        rows = sa.table(table, sa.column(column), schema=CATALOG)
        op.execute(sa.delete(rows).where(rows.c[column] == SCREEN))
    _module_description(PREVIOUS_MODULE_DESCRIPTION)
    op.drop_table(STATUSES, schema=CATALOG)
