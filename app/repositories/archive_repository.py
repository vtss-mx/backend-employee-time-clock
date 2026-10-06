"""Estado del archivo continuo del WAL (PITR) según PostgreSQL: la única consulta del monitor del servicio backup
(`app/services/pitr_monitor.py`).

Corre con el DUEÑO por la conexión directa (`DATABASE_DIRECT_URL`, como el respaldo): `pg_ls_archive_statusdir()` y
`pg_ls_waldir()` son solo de superusuario o `pg_monitor`, y la API (sin esos permisos, a propósito) nunca la usa.
Una sola sentencia y con su propio tiempo límite: lee contadores en memoria (`pg_stat_archiver`) y el directorio
`pg_wal/archive_status`, sin tocar tablas.
"""

from sqlalchemy import Connection, RowMapping, text

#: Qué tanto se archiva y qué espera: `.ready` = segmentos que PostgreSQL aún no pudo entregar al archive_command
#: (el más viejo da el lag); `pg_wal` completo, lo que ocupa en disco.
_STATS = text(
    """
    SELECT current_setting('archive_mode') AS archive_mode,
           a.archived_count, a.last_archived_wal, a.last_archived_time AS last_archived_at,
           a.failed_count, a.last_failed_wal, a.last_failed_time AS last_failed_at,
           r.ready_segments, r.oldest_ready_at,
           pg_size_bytes(current_setting('wal_segment_size')) AS segment_bytes,
           (SELECT coalesce(sum(size), 0) FROM pg_ls_waldir()) AS wal_bytes,
           now() AS checked_at
    FROM pg_stat_archiver a,
         LATERAL (SELECT count(*) AS ready_segments, min(modification) AS oldest_ready_at
                  FROM pg_ls_archive_statusdir() WHERE right(name, 6) = '.ready') r
    """
)


def archive_stats(conn: Connection, timeout_ms: int) -> RowMapping:
    """Una lectura del archivador en su transacción, con `statement_timeout` local (nunca se queda colgada)."""
    with conn.begin():
        conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(timeout_ms)})
        return conn.execute(_STATS).mappings().one()
