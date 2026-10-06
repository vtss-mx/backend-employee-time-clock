"""Tablas que crecen sin límite: particionadas por mes en PostgreSQL (regla 15 del `AGENTS.md` raíz).

Por qué: una bitácora de millones de filas en UNA tabla tiene índices cada vez más profundos, un autovacuum
que recorre todo y una depuración que borra fila por fila (y deja huecos). Partida por mes, cada consulta por
periodo lee solo los meses que le tocan (poda de particiones), cada mes tiene sus índices pequeños y lo que
vence sale con `DROP TABLE` de su partición: instantáneo, sin `DELETE` ni hinchazón.

- `PARTITIONED`: tabla → columna de tiempo (la llave de partición) y retención (None = se conserva). La
  llave primaria incluye esa columna (PostgreSQL lo exige); el ORM sigue identificando cada fila por su `id`.
- Las particiones las crea y las borra `ops.ensure_partitions` (`alembic/sql/0055_partitions.sql`), una función
  `SECURITY DEFINER` del dueño de la base: la API no tiene permisos de DDL (regla de mínimo privilegio) y solo
  puede ejecutar esa función, que únicamente acepta estas tablas. El mantenimiento la llama en cada vuelta
  (`maintenance_service`): deja creados `PARTITION_MONTHS_AHEAD` meses por delante y borra los meses que ya
  vencieron. Cada tabla tiene además su partición `_default` (nunca se pierde una fila si el mantenimiento no
  corrió) y, si ya tenía datos al particionarse, su partición `_legacy` (la tabla anterior, sin copiarla).
- Una tabla nueva que crece sin límite: su entrada aquí, `partitioned(...)` en su modelo, su migración (patrón de
  `0055_partitions.py`) y su nombre en la lista de la función (`backend-employee-time-clock/AGENTS.md` §3.3).

En SQLite (pruebas) no hay particiones: la tabla es normal y la retención la hace la depuración por lotes de
siempre (`PURGES`), que en PostgreSQL además limpia lo que quede en `_legacy` y `_default`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import DDL, MetaData, PrimaryKeyConstraint, event
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import DDLCompiler

from app.core.config import settings
from app.core.sql_safety import sql_identifier

#: Llave en `Table.info` con la columna de partición (la lee la regla de la llave primaria de abajo).
PARTITION_KEY: Final = "partition_key"


@dataclass(frozen=True)
class Partitioned:
    """Una tabla particionada por mes: su columna de tiempo y cuántos días se conserva (None: siempre)."""

    column: str
    retention_days: Callable[[], int] | None = None


#: Las tablas particionadas y su retención (la misma configuración que su depuración por lotes).
PARTITIONED: Final[dict[str, Partitioned]] = {
    "attendance.verification_logs": Partitioned("created_at"),
    "attendance.attendance_events": Partitioned("occurred_at"),
    "ops.face_attempt_metrics": Partitioned("created_at", lambda: settings.FACE_METRICS_RETENTION_DAYS),
    "ops.risk_assessments": Partitioned("created_at", lambda: settings.FACE_METRICS_RETENTION_DAYS),
    "ops.error_occurrences": Partitioned("occurred_at", lambda: settings.ERROR_OCCURRENCE_RETENTION_DAYS),
    "ops.usage_routes": Partitioned("day", lambda: settings.USAGE_DETAIL_RETENTION_DAYS),
    "ops.usage_users": Partitioned("day", lambda: settings.USAGE_DETAIL_RETENTION_DAYS),
    "ops.storage_snapshots": Partitioned("day", lambda: settings.USAGE_RETENTION_DAYS),
    "ops.perf_minutes": Partitioned("minute", lambda: settings.PERF_MINUTE_RETENTION_DAYS),
}


def partitioned(column: str) -> dict[str, Any]:
    """Argumentos de `__table_args__` de una tabla particionada por mes en `column` (solo PostgreSQL)."""
    return {"postgresql_partition_by": f"RANGE ({sql_identifier(column)})", "info": {PARTITION_KEY: column}}


@compiles(PrimaryKeyConstraint, "postgresql")
def _primary_key_with_partition(constraint: PrimaryKeyConstraint, compiler: DDLCompiler, **kw: Any) -> str:
    """En PostgreSQL la llave primaria de una tabla particionada debe incluir la llave de partición: se agrega
    aquí (en SQLite la llave sigue siendo solo `id`, autoincremental). El ORM identifica la fila por `id`."""
    key = constraint.table.info.get(PARTITION_KEY)
    if key is None or key in constraint.columns:
        return compiler.visit_primary_key_constraint(constraint, **kw)
    names = [compiler.preparer.quote(column.name) for column in constraint.columns] + [compiler.preparer.quote(key)]
    name = compiler.preparer.format_constraint(constraint)
    return f"CONSTRAINT {name} PRIMARY KEY ({', '.join(names)})"


def register_partitioned_tables(metadata: MetaData) -> None:
    """Modelos (`create_all` de pruebas y de quality.sh): la partición `_default` de cada tabla particionada, como
    la deja la migración (la función de mantenimiento la crea `alembic/sql`, ver `app/core/db_sql.py`)."""
    for name in PARTITIONED:
        table = metadata.tables[name]
        default = f"{table.schema}.{table.name}_default"
        ddl = f"CREATE TABLE IF NOT EXISTS {sql_identifier(default)} PARTITION OF {sql_identifier(name)} DEFAULT"
        event.listen(table, "after_create", DDL(ddl).execute_if(dialect="postgresql"))
