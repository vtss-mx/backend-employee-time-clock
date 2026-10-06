#!/bin/bash
# Simulacro de RECUPERACIÓN A UN PUNTO EN EL TIEMPO (PITR) de punta a punta (README "Respaldos y restauración" →
# "Recuperación a un punto en el tiempo"): una recuperación que nunca se ensayó no es una recuperación.
#
# Todo AISLADO y desechable: su propia red, un bucket FALSO (fsouza/fake-gcs-server, nunca el bucket real) detrás de
# scripts/fake_gcs_proxy.py (agrega la cabecera de la subida reanudable que el falso no manda: el simulacro sube por
# partes como contra Google), su base, su servicio pitr y sus volúmenes, con nombres únicos
# (timeclock-pitr-check-<pid>-*). Nada de puertos publicados ni del .env de trabajo. Se borra todo al terminar.
#
#   docker build --target db -t time-clock-db backend-employee-time-clock             # la imagen de los servicios db/pitr
#   docker build --target backup -t time-clock-backend-backup backend-employee-time-clock  # la del monitor (fase 3)
#   scripts/db_pitr_check.sh                         # ≈1 GB de datos (PITR_CHECK_SIZE_MB) y la fase del guardián
#   PITR_CHECK_SIZE_MB=256 PITR_CHECK_GUARD=0 scripts/db_pitr_check.sh               # rápido, sin la fase del guardián
#
# Qué demuestra (el resumen sale al final; con PITR_CHECK_REPORT=archivo también se guarda):
#   1. Respaldo base COMPLETO (el servicio pitr real, `timeclock-pgbackrest scheduler`) de ≈PITR_CHECK_SIZE_MB y un
#      DIFERENCIAL; el WAL se archiva solo (archive_command real, asíncrono y cifrado).
#   2. Filas «antes», se anota el instante T, filas «después»; RPO medido: cuánto tardó la última confirmación en estar
#      en el bucket (con PITR_ARCHIVE_TIMEOUT_SECONDS, el de producción salvo que se cambie).
#   3. Restauración a T en un contenedor nuevo (volumen vacío, archive_mode=off: jamás escribe en el repositorio):
#      EXACTAMENTE las filas hasta T; RTO = restaurar + reproducir el WAL hasta aceptar escrituras; tiempo por GB.
#   4. Restauración al último WAL archivado: todas las filas (lo que pierde un desastre total es a lo sumo el RPO).
#   5. Guardián del disco (PITR_CHECK_GUARD=1): el bucket se cae, el WAL se acumula, el monitor del servicio backup avisa
#      (lag, WAL acumulado), al pasar de PITR_WAL_MAX_MB el WAL se descarta (la base sigue aceptando escrituras y pg_wal
#      no crece sin límite), el monitor avisa el descarte; el bucket vuelve, el servicio pitr toma un respaldo COMPLETO
#      solo y el aviso se resuelve.
set -euo pipefail
IMAGE="${PITR_IMAGE:-time-clock-db}"
MONITOR_IMAGE="${PITR_MONITOR_IMAGE:-time-clock-backend-backup}"
GCS_IMAGE="${PITR_GCS_IMAGE:-fsouza/fake-gcs-server:1.56.1}"
SIZE_MB="${PITR_CHECK_SIZE_MB:-1024}"
GUARD="${PITR_CHECK_GUARD:-1}"
ARCHIVE_TIMEOUT="${PITR_CHECK_ARCHIVE_TIMEOUT_SECONDS:-60}"
ROWS=$((SIZE_MB * 1700))  # filas de ≈0.6 MB por millar (con su índice): ≈SIZE_MB de base
RUN="timeclock-pitr-check-$$"
NET="$RUN"
BUCKET=pitr-check
PASS="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
REPORT="$(mktemp)"

cleanup() {
  docker rm -f "$RUN-db" "$RUN-pitr" "$RUN-restore" "$RUN-gcs" "$RUN-gcsd" >/dev/null 2>&1 || true
  docker volume rm "$RUN-pgdata" "$RUN-socket" "$RUN-state" "$RUN-restored" "$RUN-gcsdata" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  [ -z "${PITR_CHECK_REPORT:-}" ] || cp "$REPORT" "$PITR_CHECK_REPORT"
  rm -f "$REPORT"
}
trap cleanup EXIT

say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
elapsed() { python3 -c "import sys; print(f'{float(sys.argv[2]) - float(sys.argv[1]):.1f}')" "$1" "$2"; }
psql_on() { local name=$1; shift; docker exec -i "$name" psql -U timeclock -d timeclock -qAt -v ON_ERROR_STOP=1 "$@"; }
status() { docker exec "$RUN-pitr" jq -r "$1" /var/lib/timeclock-pitr/scheduler.json 2>/dev/null || echo null; }
wait_until() {  # wait_until <segundos> <descripción> <comando...>
  local limit=$1 what=$2 deadline
  shift 2
  deadline=$(($(date +%s) + limit))
  until "$@"; do
    [ "$(date +%s)" -lt "$deadline" ] || { echo "✖ No se cumplió en ${limit} s: $what" >&2; docker logs --tail 40 "$RUN-pitr" >&2 || true; exit 1; }
    sleep 2
  done
}

# Variables de los servicios db/pitr (las mismas del .env de producción) y, SOLO en el simulacro, el bucket falso:
# las variables PGBACKREST_* ganan sobre el archivo de configuración (punto final, sin TLS, token fijo) y start-fast
# no espera al siguiente checkpoint.
COMMON=(
  -e POSTGRES_USER=timeclock -e POSTGRES_PASSWORD=pitr-check -e POSTGRES_DB=timeclock
  -e PITR_ENABLED=true -e "PITR_CIPHER_PASS=$PASS" -e "GCS_BUCKET=$BUCKET" -e GCS_PREFIX=check
  -e "PITR_ARCHIVE_TIMEOUT_SECONDS=$ARCHIVE_TIMEOUT" -e PG_MEMORY_MB=1024 -e PG_CPUS=2
  -e "PGBACKREST_REPO1_GCS_ENDPOINT=http://$RUN-gcs:4443" -e PGBACKREST_REPO1_GCS_KEY_TYPE=token
  -e PGBACKREST_REPO1_GCS_KEY=pitr-check -e PGBACKREST_START_FAST=y
)

start_db() {  # start_db [variables extra...]: el servicio db como en docker compose (init, socket y estado compartidos)
  docker run -d --init --name "$RUN-db" --network "$NET" --shm-size=256m \
    -v "$RUN-pgdata:/var/lib/postgresql/data" -v "$RUN-socket:/var/run/postgresql" \
    -v "$RUN-state:/var/lib/timeclock-pitr" "${COMMON[@]}" "$@" "$IMAGE" >/dev/null
  wait_until 120 "la base acepta conexiones" docker exec "$RUN-db" pg_isready -q -U timeclock -d timeclock
}

echo "== Preparación: red, bucket falso y base (≈${SIZE_MB} MB) =="
docker network create "$NET" >/dev/null
docker run -d --name "$RUN-gcsd" --network "$NET" -v "$RUN-gcsdata:/storage" "$GCS_IMAGE" \
  -scheme http -port 4443 -backend filesystem -filesystem-root /storage -external-url "http://$RUN-gcs:4443" >/dev/null
# "El bucket" para pgBackRest es el proxy: detenerlo es la caída del bucket (fase 5).
docker run -d --name "$RUN-gcs" --network "$NET" -v "$(cd "$(dirname "$0")" && pwd)/fake_gcs_proxy.py:/proxy.py:ro" \
  --entrypoint python "$MONITOR_IMAGE" /proxy.py "$RUN-gcsd:4443" 4443 >/dev/null
start_db
wait_until 60 "el bucket falso responde" docker exec "$RUN-db" wget -q -O /dev/null \
  --header 'Content-Type: application/json' --post-data "{\"name\":\"$BUCKET\"}" \
  "http://$RUN-gcs:4443/storage/v1/b?project=pitr-check"
psql_on "$RUN-db" <<SQL
CREATE TABLE bulk (id bigint PRIMARY KEY, payload text NOT NULL);
-- ≈0.5 KB por fila de texto que no se repite (16 md5 distintos): comprime como datos reales (≈2:1), no 15:1.
INSERT INTO bulk SELECT g, concat(md5(g::text), md5((g * 3)::text), md5((g * 5)::text), md5((g * 7)::text),
  md5((g * 11)::text), md5((g * 13)::text), md5((g * 17)::text), md5((g * 19)::text), md5((g * 23)::text),
  md5((g * 29)::text), md5((g * 31)::text), md5((g * 37)::text), md5((g * 41)::text), md5((g * 43)::text),
  md5((g * 47)::text), md5((g * 53)::text)) FROM generate_series(1, $ROWS) g;
CREATE TABLE drill_events (id bigserial PRIMARY KEY, batch text NOT NULL, at timestamptz NOT NULL DEFAULT clock_timestamp());
CHECKPOINT;
SQL
DB_BYTES=$(psql_on "$RUN-db" -c "SELECT pg_database_size('timeclock')")
say "Base de origen: $(python3 -c "print(f'{$DB_BYTES / 1048576:.2f}')") MB"

echo "== 1. Servicio pitr: stanza, check y respaldo base completo =="
docker run -d --init --name "$RUN-pitr" --network "$NET" -u postgres \
  -v "$RUN-pgdata:/var/lib/postgresql/data:ro" -v "$RUN-socket:/var/run/postgresql" \
  -v "$RUN-state:/var/lib/timeclock-pitr" "${COMMON[@]}" -e PITR_CHECK_SECONDS=5 -e PITR_RETRY_MINUTES=1 \
  --entrypoint timeclock-pgbackrest "$IMAGE" scheduler >/dev/null
started=$(now)
full_ready() { [ "$(status '.last_full_at')" != null ]; }
wait_until 1800 "el primer respaldo completo" full_ready
FULL_SECONDS=$(status '.last_backup_seconds')
REPO_MB=$(docker exec "$RUN-pitr" jq -r '.repo_bytes / 1048576 * 100 | floor / 100' /var/lib/timeclock-pitr/scheduler.json)
say "Respaldo completo: ${FULL_SECONDS} s (servicio pitr arriba → respaldo listo: $(elapsed "$started" "$(now)") s), ${REPO_MB} MB en el bucket (comprimido y cifrado)"
# Nada legible en el bucket (aes-256-cbc antes de subir): cada archivo entero (WAL, manifiestos, info) es un sobre de
# OpenSSL ("Salted__"); los paquetes (bundle/) y los mapas de bloques (.pgbi) llevan cada pieza cifrada por separado, así
# que se revisa que ninguno empiece como zstd sin cifrar (28 b5 2f fd). Y con otra llave el repositorio no abre.
OBJECTS=$(docker exec "$RUN-gcsd" sh -c "find /storage/$BUCKET/check/pitr -type f | wc -l")
unencrypted() {  # lee nombres de archivos del bucket falso e imprime los que no están cifrados
  local file
  while read -r file; do
    case "$file" in
      */bundle/* | *.pgbi)
        [ "$(docker exec "$RUN-gcsd" sh -c "head -c 4 '$file' | od -An -tx1 | tr -d ' '")" != 28b52ffd ] || echo "$file" ;;
      *) [ "$(docker exec "$RUN-gcsd" head -c 8 "$file")" = Salted__ ] || echo "$file" ;;
    esac
  done
}
PLAIN=$(docker exec "$RUN-gcsd" sh -c "find /storage/$BUCKET/check/pitr -type f" | unencrypted | wc -l | tr -d ' ')
WRONG=$(docker run --rm --network "$NET" -u postgres "${COMMON[@]}" -e "PITR_CIPHER_PASS=${PASS}x" \
  --entrypoint timeclock-pgbackrest "$IMAGE" info --output=json 2>/dev/null | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin)[0]["status"]["code"])
except Exception:
    print("ilegible")' || true)
[ "$WRONG" != 0 ] || { echo "✖ El repositorio se abrió con otra llave" >&2; exit 1; }
say "Objetos del repositorio en el bucket: $OBJECTS, sin cifrar: $PLAIN (debe ser 0); con otra llave: estado $WRONG (no abre)"
[ "$PLAIN" = 0 ] && [ "$OBJECTS" -gt 0 ] || { echo "✖ El bucket tiene objetos sin cifrar" >&2; exit 1; }

echo "== 2. Filas «antes», instante T, filas «después» =="
psql_on "$RUN-db" -c "INSERT INTO drill_events (batch) SELECT 'antes' FROM generate_series(1, 1000)"
docker exec "$RUN-pitr" timeclock-pgbackrest backup diff >/dev/null 2>&1
say "Respaldo diferencial: $(status '.last_backup_seconds') s (tipo $(status '.last_backup_type'))"
psql_on "$RUN-db" -c "INSERT INTO drill_events (batch) SELECT 'antes' FROM generate_series(1, 234)"
sleep 1
TARGET=$(psql_on "$RUN-db" -c "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') || '+00'")
sleep 1
committed=$(now)
WAL=$(psql_on "$RUN-db" -c "INSERT INTO drill_events (batch) SELECT 'después' FROM generate_series(1, 500); SELECT pg_walfile_name(pg_current_wal_lsn())" | tail -n 1)
# RPO: la última confirmación está a salvo cuando su segmento de WAL ya está en el bucket (archive_timeout lo cierra).
wait_until $((ARCHIVE_TIMEOUT * 3 + 120)) "el segmento $WAL en el bucket" sh -c \
  "[ \"\$(docker exec -i $RUN-db psql -U timeclock -d timeclock -qAt -c \"SELECT coalesce(last_archived_wal, '') >= '$WAL' FROM pg_stat_archiver\")\" = t ]"
RPO=$(elapsed "$committed" "$(now)")
say "Instante T = $TARGET"
say "RPO medido: ${RPO} s (archive_timeout = ${ARCHIVE_TIMEOUT} s; la confirmación de las filas «después» tardó eso en estar en el bucket)"

recovered() {  # la copia ya acepta escrituras (y, si su contenedor murió, se falla de inmediato con su log)
  if [ "$(docker inspect -f '{{.State.Running}}' "$RUN-restore" 2>/dev/null)" != true ]; then
    docker logs --tail 30 "$RUN-restore" >&2 || true
    echo "✖ La copia restaurada no arrancó" >&2
    exit 1
  fi
  [ "$(docker exec -i "$RUN-restore" psql -U timeclock -d timeclock -qAt -c 'SELECT NOT pg_is_in_recovery()' 2>/dev/null)" = t ]
}

restore_and_check() {  # restore_and_check <etiqueta> <antes esperadas> <después esperadas> [--target T]
  local label=$1 before=$2 after=$3 t0 t1 t2 counts bulk
  shift 3
  docker rm -f "$RUN-restore" >/dev/null 2>&1 || true
  docker volume rm "$RUN-restored" >/dev/null 2>&1 || true
  t0=$(now)
  docker run --rm --network "$NET" -u postgres -v "$RUN-restored:/var/lib/postgresql/data" "${COMMON[@]}" \
    --entrypoint timeclock-pgbackrest "$IMAGE" restore --archive-off "$@" > /dev/null
  t1=$(now)
  # La copia arranca con start.sh (los MISMOS parámetros: PostgreSQL no recupera con max_connections menor que el
  # original) pero con PITR_ENABLED=false: sin archive_command; solo lee el WAL del repositorio hasta su objetivo.
  docker run -d --init --name "$RUN-restore" --network "$NET" --shm-size=256m -v "$RUN-restored:/var/lib/postgresql/data" \
    "${COMMON[@]}" -e PITR_ENABLED=false --entrypoint sh "$IMAGE" \
    -c 'timeclock-pgbackrest config && exec timeclock-postgres.sh' >/dev/null
  wait_until 1800 "la copia $label termina de recuperarse" recovered
  t2=$(now)
  counts=$(psql_on "$RUN-restore" -c "SELECT count(*) FILTER (WHERE batch = 'antes') || '/' || count(*) FILTER (WHERE batch = 'después') FROM drill_events")
  bulk=$(psql_on "$RUN-restore" -c "SELECT count(*) FROM bulk")
  say "Restauración $label: pgbackrest restore $(elapsed "$t0" "$t1") s + reproducir el WAL $(elapsed "$t1" "$t2") s = RTO $(elapsed "$t0" "$t2") s; filas antes/después = $counts (esperadas $before/$after), bulk = $bulk"
  [ "$counts" = "$before/$after" ] && [ "$bulk" = "$ROWS" ] || { echo "✖ La copia $label no tiene las filas esperadas" >&2; exit 1; }
  # Lo que vale para el tiempo por GB: restaurar + recuperar, sobre el tamaño de la base de origen.
  RTO_LAST=$(elapsed "$t0" "$t2")
  docker rm -f "$RUN-restore" >/dev/null
}

echo "== 3. Restauración al instante T (copia aislada, archive_mode=off) =="
restore_and_check "a T" 1234 0 --target "$TARGET"
say "Tiempo de restauración por GB: $(python3 -c "print(f'{$RTO_LAST / ($DB_BYTES / 1073741824):.1f}')") s/GB"

echo "== 4. Restauración al último WAL archivado =="
restore_and_check "al último WAL" 1234 500

if [ "$GUARD" = 1 ]; then
  echo "== 5. Guardián del disco: el bucket se cae =="
  # La base vuelve a arrancar con un guardián chico para verlo actuar (los demás valores, los de producción).
  docker rm -f "$RUN-db" >/dev/null
  start_db -e PITR_WAL_MAX_MB=256 -e PITR_PUSH_BATCH_MB=64 -e PGBACKREST_ARCHIVE_TIMEOUT=5
  docker stop "$RUN-gcs" >/dev/null
  monitor() {  # monitor <texto esperado>: el monitor REAL del servicio backup (app/services/pitr_monitor.py)
    local out
    out=$(docker run --rm --network "$NET" -v "$RUN-state:/var/lib/timeclock-pitr:ro" --env-file <(docker run --rm \
      --entrypoint python "$MONITOR_IMAGE" scripts/generate_secrets.py) \
      -e PITR_ENABLED=true -e PITR_WAL_MAX_MB=256 -e PITR_WAL_ALERT_MB=64 -e PITR_LAG_ALERT_SECONDS=30 \
      -e PITR_RETRY_MINUTES=1 -e "DATABASE_DIRECT_URL=postgresql+psycopg://timeclock:pitr-check@$RUN-db:5432/timeclock" \
      -e "DATABASE_URL=postgresql+psycopg://timeclock:pitr-check@$RUN-db:5432/timeclock" \
      --entrypoint python "$MONITOR_IMAGE" -m app.cli db pitr-status 2>&1 || true)
    printf '%s\n' "$out" | tee -a "$REPORT"
    case "$out" in *"$1"*) ;; *) echo "✖ El monitor no dijo: $1" >&2; exit 1 ;; esac
  }
  peak=0
  for round in $(seq 1 40); do
    psql_on "$RUN-db" -c "INSERT INTO drill_events (batch) SELECT 'guardián' FROM generate_series(1, 2000); SELECT pg_switch_wal()" >/dev/null
    ready=$(psql_on "$RUN-db" -c "SELECT count(*) FROM pg_ls_archive_statusdir() WHERE right(name, 6) = '.ready'")
    [ "$ready" -le "$peak" ] || peak=$ready
    [ "$round" != 8 ] || { say "-- Monitor con el bucket caído (antes del límite):"; monitor "AVISO wal_backlog"; }
    if docker exec "$RUN-db" test -f /var/lib/timeclock-pitr/wal-dropped.json; then break; fi
    sleep 1
  done
  wait_until 300 "el guardián descarta el WAL" docker exec "$RUN-db" test -f /var/lib/timeclock-pitr/wal-dropped.json
  sleep 5
  ready=$(psql_on "$RUN-db" -c "SELECT count(*) FROM pg_ls_archive_statusdir() WHERE right(name, 6) = '.ready'")
  wal_mb=$(psql_on "$RUN-db" -c "SELECT (sum(size) / 1048576)::int FROM pg_ls_waldir()")
  writes=$(psql_on "$RUN-db" -c "INSERT INTO drill_events (batch) VALUES ('sigue viva') RETURNING 'sí'")
  say "Guardián: pico de ${peak} segmentos esperando ($((peak * 16)) MB, límite 256 MB + tanda 64 MB); tras descartar: $ready esperando, pg_wal = ${wal_mb} MB, la base acepta escrituras: $writes"
  say "Aviso de descarte: $(docker exec "$RUN-db" cat /var/lib/timeclock-pitr/wal-dropped.json)"
  # Con init (tini) como PID 1, el proceso asíncrono de pgBackRest que termina con error (bucket caído) NUNCA es
  # tomado por PostgreSQL como un backend caído: sin init, el postmaster reiniciaría todas las conexiones.
  crashes=$(docker logs "$RUN-db" 2>&1 | grep -c "terminating any other active server processes" || true)
  say "Reinicios de PostgreSQL por procesos huérfanos con el bucket caído: $crashes (debe ser 0)"
  [ "$crashes" = 0 ] || { echo "✖ PostgreSQL se reinició: ¿falta init en el servicio db?" >&2; exit 1; }
  say "-- Monitor tras el descarte:"
  monitor "AVISO wal_dropped"
  echo "== El bucket vuelve: el servicio pitr toma un respaldo completo solo =="
  docker start "$RUN-gcs" >/dev/null
  wait_until 900 "el respaldo completo que repara el descarte" docker exec "$RUN-db" test -f /var/lib/timeclock-pitr/wal-dropped.resolved.json
  wait_until 120 "el aviso de descarte se resuelve" sh -c "! docker exec $RUN-db test -f /var/lib/timeclock-pitr/wal-dropped.json"
  say "Recuperado: respaldo $(status '.last_backup_type') en $(status '.last_backup_seconds') s, $(status '.backups') respaldos en el repositorio"
  say "-- Monitor con todo al día:"
  monitor "Sin avisos"
fi

echo
echo "================ Resumen ================"
cat "$REPORT"
echo "✔ Simulacro PITR completo"
