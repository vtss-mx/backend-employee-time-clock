#!/bin/sh
# PostgreSQL ajustado al equipo (README "Parámetros de PostgreSQL"). docker compose lo usa como entrypoint del
# servicio `db`: calcula los parámetros con las variables del `.env` del backend y arranca la imagen oficial
# (`docker-entrypoint.sh postgres -c ...`, que inicializa la base la primera vez). Un equipo más grande solo
# cambia `.env` (PG_MEMORY_MB, PG_CPUS...) y reinicia `db`; nada de esto vive en el código.
#
# Por qué cada valor (fórmulas habituales para un servidor dedicado a OLTP en SSD):
# - memoria: shared_buffers = 25 % de PG_MEMORY_MB; effective_cache_size = 75 % (lo que el planificador supone
#   en caché del sistema); maintenance_work_mem = 1/16 (≤ 2 GB: índices CONCURRENTLY, VACUUM); work_mem = 25 %
#   repartido entre las conexiones reales que puede abrir PgBouncer (PGBOUNCER_MAX_DB_CONNECTIONS; ≥ 4 MB, ≤ 256 MB).
# - SSD: random_page_cost 1.1 (4 supone discos mecánicos y castiga los índices) y effective_io_concurrency 200.
# - escritura: checkpoints repartidos (0.9) y un max_wal_size holgado: menos checkpoints forzados bajo carga.
# - autovacuum: un trabajador por cada 2 CPU (3 a 8), cada 15 s y con más presupuesto de E/S (SSD): las tablas
#   de mucho movimiento (sesiones, contadores, consumo) no se hinchan; umbrales relativos menores (5 % / 2 %).
# - paralelismo por CPU; pg_stat_statements (qué consultas consumen la base) y track_io_timing.
# - registro: consultas de más de PG_LOG_MIN_DURATION_MS, esperas de candados y autovacuum largos.
# - idle_in_transaction_session_timeout de 10 min para TODA sesión (respaldo); la API tiene su propio límite por
#   rol (DB_IDLE_IN_TRANSACTION_TIMEOUT_MS, app/core/db_roles.py).
# - jit=off: las consultas de la API duran milisegundos y compilar con JIT cuesta 20-50 ms (medido con
#   perf/db/run.sh: "¿ya se checó en el sitio?" pasaba de 24 a 45 ms solo por compilar).
set -eu
MEMORY_MB="${PG_MEMORY_MB:-2048}"
CPUS="${PG_CPUS:-4}"
ACTIVE="${PGBOUNCER_MAX_DB_CONNECTIONS:-60}"

clamp() { value=$1; low=$2; high=$3; [ "$value" -lt "$low" ] && value=$low; [ "$value" -gt "$high" ] && value=$high; echo "$value"; }
WORK_MEM_KB=$(clamp $((MEMORY_MB * 1024 / 4 / ACTIVE)) 4096 262144)
MAINTENANCE_MB=$(clamp $((MEMORY_MB / 16)) 64 2048)
PARALLEL=$(clamp $((CPUS / 2)) 1 4)
VACUUM_WORKERS=$(clamp $((CPUS / 2)) 3 8)
WORKERS=$(clamp "$CPUS" 8 64)

# Recuperación a un punto en el tiempo (postgres/pgbackrest.sh; README "Respaldos y restauración"): con PITR_ENABLED=true
# cada segmento de WAL se archiva CIFRADO al bucket (pgBackRest asíncrono, con el guardián del disco) y archive_timeout
# acota cuánto puede esperar un cambio antes de salir (el RPO). Una configuración incompleta (sin bucket o sin
# PITR_CIPHER_PASS) NUNCA deja la base abajo: arranca sin archivar, lo dice aquí y el servicio backup avisa al ADMIN
# (archive_mode apagado con PITR encendido). La imagen oficial sin pgBackRest (sin la etapa `db` del Dockerfile)
# arranca igual que antes.
if command -v timeclock-pgbackrest >/dev/null 2>&1 && timeclock-pgbackrest enabled; then
  if timeclock-pgbackrest config; then
    set -- -c archive_mode=on -c "archive_command=/usr/local/bin/timeclock-pgbackrest archive %p" \
      -c "archive_timeout=$(timeclock-pgbackrest archive-timeout)" "$@"
  else
    echo "PITR_ENABLED=true pero pgBackRest no quedó configurado: PostgreSQL arranca SIN archivar el WAL" >&2
  fi
fi

exec docker-entrypoint.sh postgres \
  -c max_connections="${POSTGRES_MAX_CONNECTIONS:-150}" \
  -c shared_buffers="$((MEMORY_MB / 4))MB" \
  -c effective_cache_size="$((MEMORY_MB * 3 / 4))MB" \
  -c maintenance_work_mem="${MAINTENANCE_MB}MB" \
  -c work_mem="${WORK_MEM_KB}kB" \
  -c wal_buffers=16MB \
  -c checkpoint_completion_target=0.9 \
  -c max_wal_size="${PG_MAX_WAL_SIZE_MB:-4096}MB" \
  -c min_wal_size=1GB \
  -c random_page_cost="${PG_RANDOM_PAGE_COST:-1.1}" \
  -c effective_io_concurrency="${PG_EFFECTIVE_IO_CONCURRENCY:-200}" \
  -c max_worker_processes="$WORKERS" \
  -c max_parallel_workers="$CPUS" \
  -c max_parallel_workers_per_gather="$PARALLEL" \
  -c max_parallel_maintenance_workers="$PARALLEL" \
  -c autovacuum_max_workers="$VACUUM_WORKERS" \
  -c autovacuum_naptime=15s \
  -c autovacuum_vacuum_cost_limit=2000 \
  -c autovacuum_vacuum_scale_factor=0.05 \
  -c autovacuum_vacuum_insert_scale_factor=0.05 \
  -c autovacuum_analyze_scale_factor=0.02 \
  -c shared_preload_libraries=pg_stat_statements \
  -c pg_stat_statements.max=10000 \
  -c pg_stat_statements.track=top \
  -c track_io_timing=on \
  -c log_min_duration_statement="${PG_LOG_MIN_DURATION_MS:-1000}" \
  -c log_lock_waits=on \
  -c log_autovacuum_min_duration=10s \
  -c idle_in_transaction_session_timeout=10min \
  -c jit=off \
  "$@"
