#!/bin/sh
# Recuperación a un punto en el tiempo (PITR) con pgBackRest: README "Respaldos y restauración" → "Recuperación a un
# punto en el tiempo" y backend AGENTS.md §3.4. Vive en la imagen `db` (Dockerfile, etapa `db`) como
# /usr/local/bin/timeclock-pgbackrest; la usan el servicio `db` (archive_command) y el servicio `pitr` (respaldos base).
#
# Decisión del dueño (2026-10-06, tras perder la base completa con el disco del equipo): archivo CONTINUO del WAL y
# respaldos base al bucket propio de la plataforma, CIFRADOS antes de salir del servidor (aes-256-cbc con
# PITR_CIPHER_PASS), para volver a cualquier segundo de los últimos PITR_RETENTION_DAYS días aunque se pierda el equipo.
#
#   timeclock-pgbackrest enabled             → éxito si PITR_ENABLED=true (postgres/start.sh decide si archiva)
#   timeclock-pgbackrest config              → escribe /etc/pgbackrest/pgbackrest.conf (600) con las variables del .env
#   timeclock-pgbackrest archive-timeout     → archive_timeout de PostgreSQL (PITR_ARCHIVE_TIMEOUT_SECONDS)
#   timeclock-pgbackrest archive <WAL>       → archive_command del servicio db (asíncrono y con el guardián del disco)
#   timeclock-pgbackrest scheduler           → servicio pitr: stanza, respaldos base, vencidos y su estado
#   timeclock-pgbackrest backup [full|diff]  → un respaldo base ahora (a mano: docker compose exec pitr ...)
#   timeclock-pgbackrest info [opciones]     → respaldos y ventana de recuperación (pgbackrest info)
#   timeclock-pgbackrest restore [--target "AAAA-MM-DD HH:MM:SS+00"] [--archive-off] [--delta]
#                                            → restaura hasta el último WAL o hasta ese instante: en un PGDATA VACÍO
#                                              o, con --delta, sobre la base detenida (README: procedimiento)
#
# Guardián del disco (regla 7: el WAL nunca llena el disco en silencio). Si el bucket no responde, el WAL espera en
# pg_wal. Al pasar de PITR_WAL_MAX_MB (más a lo sumo una tanda de PITR_PUSH_BATCH_MB), pgBackRest le dice a PostgreSQL
# que ya se archivó y lo DESCARTA (`archive-push-queue-max`): la base sigue viva y el disco no se llena, pero la
# recuperación a un punto en el tiempo queda interrumpida desde ese momento hasta el siguiente respaldo base (el
# servicio pitr lo toma completo en cuanto el bucket responde). El descarte queda en $PITR_STATUS_DIR/wal-dropped.json
# y el servicio backup avisa al ADMIN en "Errores del sistema" desde mucho antes: lag, WAL acumulado y fallas
# (app/services/pitr_monitor.py).
#
# Secretos: PITR_CIPHER_PASS solo vive en el .env y en el archivo de configuración (600, del usuario postgres, dentro
# del contenedor); nunca se imprime. La llave del bucket es la misma cuenta de servicio de las imágenes.
# Cada valor por defecto de este script es el mismo de app/core/config.py (tests/test_env_files.py lo verifica).
set -eu

STANZA="${POSTGRES_DB:-timeclock}"
DIR="${PITR_STATUS_DIR:-/var/lib/timeclock-pitr}"
CONF=/etc/pgbackrest/pgbackrest.conf
SOCKET=/var/run/postgresql
STATUS="$DIR/scheduler.json"
DROPPED="$DIR/wal-dropped.json"
: "${PGDATA:=/var/lib/postgresql/data}"  # lo fija la imagen oficial de PostgreSQL

log() { printf '%s timeclock-pgbackrest: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
fail() { log "$*"; exit 1; }
enabled() { [ "${PITR_ENABLED:-false}" = true ]; }

# Carpeta del entorno en el bucket: la misma regla de `settings.storage_prefix` (GCS_PREFIX o, vacía, ENVIRONMENT).
prefix() {
  if [ -n "${GCS_PREFIX:-}" ]; then
    printf '%s' "$GCS_PREFIX" | sed -E 's#^[ /]+##; s#[ /]+$##'
    return
  fi
  value=$(printf '%s' "${ENVIRONMENT:-development}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^-+//; s/-+$//')
  value=$(printf '%s' "$value" | cut -c1-40)
  printf '%s' "${value:-default}"
}

bucket() { printf '%s' "${GCS_BUCKET:-}" | sed -E 's/^ +//; s/ +$//; s#^gs://##; s#^/+##; s#/+$##'; }

# Un JSON escrito de golpe (temporal + rename): quien lo lee (el servicio backup) nunca ve uno a medias. 644: lo lee
# otro usuario (el del servicio backup) desde el mismo volumen, de solo lectura. Nunca lleva secretos.
write_json() {
  file=$1
  shift
  tmp=$(mktemp "$file.XXXXXX")
  if jq -n "$@" > "$tmp"; then
    chmod 644 "$tmp"
    mv -f "$tmp" "$file"
  else
    rm -f "$tmp"
    return 1
  fi
}

config() {
  : "${PITR_CIPHER_PASS:?Falta PITR_CIPHER_PASS en el .env (la genera python3 scripts/generate_secrets.py)}"
  name=$(bucket)
  [ -n "$name" ] || fail "Falta GCS_BUCKET en el .env: el WAL y los respaldos base van al bucket de la plataforma"
  mkdir -p "$DIR/spool"
  if [ "$(id -u)" = 0 ]; then
    # El servicio db prepara todo como root (postgres/start.sh) antes de bajar al usuario postgres.
    chown postgres:postgres "$DIR" "$DIR/spool"
    chmod 755 "$DIR"
  fi
  key=""
  [ -z "${GCS_CREDENTIALS_FILE:-}" ] || key="repo1-gcs-key=$GCS_CREDENTIALS_FILE"
  umask 077
  tmp=$(mktemp /etc/pgbackrest/.pgbackrest.conf.XXXXXX)
  # Por qué cada opción (README "Recuperación a un punto en el tiempo"):
  # - repo1: el bucket propio, bajo <GCS_PREFIX>/pitr; cifrado del lado del servidor ANTES de subir (aes-256-cbc).
  # - retención por TIEMPO: siempre hay respaldos para volver a cualquier instante de los últimos PITR_RETENTION_DAYS
  #   días; el WAL más viejo que el respaldo completo más viejo que se conserva se borra solo (expire tras cada respaldo).
  # - bundle + block: menos objetos en el bucket y diferenciales por bloque (más chicos); zst comprime rápido.
  # - archive-async: el WAL se sube en paralelo (process-max) y PostgreSQL no espera a la red por cada segmento.
  # - archive-push-queue-max / batch-size: el guardián del disco (arriba).
  # - start-fast=n: el respaldo base espera el siguiente checkpoint programado en lugar de forzar uno (sin picos de E/S).
  # - log-level-file=off: nada de bitácoras que crecen en disco; todo va al log del contenedor (y el de PostgreSQL).
  cat > "$tmp" <<EOF
# Generado por timeclock-pgbackrest al arrancar a partir del .env (no se edita: se vuelve a escribir).
[global]
repo1-type=gcs
repo1-gcs-bucket=$name
repo1-gcs-key-type=service
$key
repo1-path=/$(prefix)/pitr
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=$PITR_CIPHER_PASS
repo1-retention-full-type=time
repo1-retention-full=${PITR_RETENTION_DAYS:-14}
repo1-bundle=y
repo1-block=y
compress-type=zst
process-max=${PITR_PROCESS_MAX:-2}
io-timeout=${PITR_IO_TIMEOUT_SECONDS:-60}
archive-async=y
spool-path=$DIR/spool
archive-push-queue-max=${PITR_WAL_MAX_MB:-8192}MiB
archive-push-batch-size=${PITR_PUSH_BATCH_MB:-1024}MiB
start-fast=n
log-level-file=off
log-level-console=warn

[global:backup]
log-level-console=info

[global:restore]
log-level-console=info

[$STANZA]
pg1-path=$PGDATA
pg1-socket-path=$SOCKET
pg1-port=5432
pg1-user=${POSTGRES_USER:-timeclock}
EOF
  [ "$(id -u)" != 0 ] || chown postgres:postgres "$tmp"
  mv -f "$tmp" "$CONF"
}

# ------------------------------------------------------------------------------------------------- servicio db

archive() {
  wal="${1:?Uso: timeclock-pgbackrest archive <ruta del WAL>}"
  if output=$(pgbackrest --stanza="$STANZA" archive-push "$wal" 2>&1); then code=0; else code=$?; fi
  # Lo que diga pgBackRest (solo avisos y errores) queda en el log de PostgreSQL.
  [ -z "$output" ] || printf '%s\n' "$output" >&2
  case "$output" in
    *"dropped WAL file"*) mark_dropped "$(basename "$wal")" || log "No se pudo anotar el WAL descartado ($wal)" ;;
  esac
  return "$code"
}

# El guardián descartó WAL: se anota cuándo empezó (para avisar y para saber qué respaldo base lo repara).
mark_dropped() {
  now=$(date +%s)
  first=$(jq -r '.first // empty' "$DROPPED" 2>/dev/null || true)
  count=$(jq -r '.count // 0' "$DROPPED" 2>/dev/null || echo 0)
  write_json "$DROPPED" --argjson first "${first:-$now}" --argjson last "$now" --argjson count "$((count + 1))" \
    --arg wal "$1" '{first: $first, last: $last, count: $count, last_wal: $wal}'
}

# ------------------------------------------------------------------------------------------------- servicio pitr

STATE=starting
LAST_ERROR=""
LAST_ERROR_AT=null
LAST_FULL_AT=null
LAST_FULL_START=null
LAST_BACKUP_AT=null
LAST_BACKUP_TYPE=""
LAST_BACKUP_SECONDS=null
OLDEST_BACKUP_AT=null
BACKUPS=0
REPO_BYTES=0
WAL_MIN=""
WAL_MAX=""
RUNNING_SINCE=null
RUNNING_TYPE=""

save_status() {
  write_json "$STATUS" --arg state "$STATE" --argjson updated_at "$(date +%s)" \
    --arg last_error "$LAST_ERROR" --argjson last_error_at "$LAST_ERROR_AT" \
    --argjson last_full_at "$LAST_FULL_AT" --argjson last_backup_at "$LAST_BACKUP_AT" \
    --arg last_backup_type "$LAST_BACKUP_TYPE" --argjson last_backup_seconds "$LAST_BACKUP_SECONDS" \
    --argjson oldest_backup_at "$OLDEST_BACKUP_AT" --argjson backups "$BACKUPS" --argjson repo_bytes "$REPO_BYTES" \
    --arg wal_min "$WAL_MIN" --arg wal_max "$WAL_MAX" \
    --argjson running_since "$RUNNING_SINCE" --arg running_type "$RUNNING_TYPE" \
    --argjson check_seconds "${PITR_CHECK_SECONDS:-60}" \
    '{updated_at: $updated_at, state: $state, last_error: $last_error, last_error_at: $last_error_at,
      last_full_at: $last_full_at, last_backup_at: $last_backup_at, last_backup_type: $last_backup_type,
      last_backup_seconds: $last_backup_seconds, oldest_backup_at: $oldest_backup_at, backups: $backups,
      repo_bytes: $repo_bytes, wal_min: $wal_min, wal_max: $wal_max, running_since: $running_since,
      running_type: $running_type, check_seconds: $check_seconds}' \
    || log "No se pudo escribir el estado ($STATUS)"
}

failed() {
  STATE=error
  LAST_ERROR=$(printf '%s' "$1" | tr '\n' ' ' | tail -c 800)
  LAST_ERROR_AT=$(date +%s)
  log "$LAST_ERROR"
}

# pgbackrest <comando> con tiempo límite, en segundo plano para que un SIGTERM del contenedor lo detenga (trap).
run() {
  out=$(mktemp)
  timeout -s TERM "${PITR_BACKUP_TIMEOUT_SECONDS:-21600}" pgbackrest --stanza="$STANZA" "$@" > "$out" 2>&1 &
  if wait $!; then code=0; else code=$?; fi
  cat "$out" >&2
  [ "$code" -eq 0 ] || failed "pgbackrest $* terminó con $code: $(tail -c 600 "$out")"
  rm -f "$out"
  return "$code"
}

# Lo que dice el repositorio: último respaldo completo y cualquiera, el más viejo (inicio de la ventana de
# recuperación), cuántos, tamaño en el bucket y el rango del WAL archivado.
refresh_info() {
  if ! info=$(timeout -s TERM 300 pgbackrest --stanza="$STANZA" --output=json info 2>&1); then
    failed "pgbackrest info: $info"
    return 1
  fi
  code=$(printf '%s' "$info" | jq -r '.[0].status.code // 99') || code=99
  # 0 = bien; 2 = la stanza existe pero aún no hay respaldos (el primero se toma ahora).
  if [ "$code" != 0 ] && [ "$code" != 2 ]; then
    failed "pgbackrest info: $(printf '%s' "$info" | jq -r '.[0].repo[0].status.message // .[0].status.message // "respuesta ilegible"' 2>/dev/null | head -c 400)"
    return 1
  fi
  pick() { printf '%s' "$info" | jq -r "$1"; }
  LAST_FULL_AT=$(pick '[.[0].backup[]? | select(.type == "full") | .timestamp.stop] | max // null')
  LAST_FULL_START=$(pick '[.[0].backup[]? | select(.type == "full") | .timestamp.start] | max // null')
  LAST_BACKUP_AT=$(pick '[.[0].backup[]? | .timestamp.stop] | max // null')
  LAST_BACKUP_TYPE=$(pick '[.[0].backup[]?] | max_by(.timestamp.stop) | .type // ""')
  OLDEST_BACKUP_AT=$(pick '[.[0].backup[]? | .timestamp.start] | min // null')
  BACKUPS=$(pick '[.[0].backup[]?] | length')
  REPO_BYTES=$(pick '[.[0].backup[]? | .info.repository.delta // 0] | add // 0')
  WAL_MIN=$(pick '.[0].archive[0].min // ""')
  WAL_MAX=$(pick '.[0].archive[0].max // ""')
}

# Qué respaldo toca: completo si no hay, si venció PITR_FULL_BACKUP_HOURS o si el guardián descartó WAL después del
# último completo; diferencial cada PITR_DIFF_BACKUP_HOURS (0 = solo completos); si no, ninguno.
due() {
  now=$(date +%s)
  if [ -f "$DROPPED" ]; then
    last_drop=$(jq -r '.last // 0' "$DROPPED" 2>/dev/null || echo 0)
    if [ "$LAST_FULL_START" = null ] || [ "$LAST_FULL_START" -le "$last_drop" ]; then
      echo full
      return
    fi
  fi
  if [ "$LAST_FULL_AT" = null ] || [ $((now - LAST_FULL_AT)) -ge $((${PITR_FULL_BACKUP_HOURS:-168} * 3600)) ]; then
    echo full
  elif [ "${PITR_DIFF_BACKUP_HOURS:-24}" -gt 0 ] \
    && [ $((now - LAST_BACKUP_AT)) -ge $((${PITR_DIFF_BACKUP_HOURS:-24} * 3600)) ]; then
    echo diff
  else
    echo none
  fi
}

backup() {
  kind=$1
  started=$(date +%s)
  STATE=running RUNNING_TYPE=$kind RUNNING_SINCE=$started
  save_status
  log "Respaldo base $kind: inicia"
  if run --type="$kind" backup; then code=0; else code=$?; fi
  STATE=ok RUNNING_TYPE="" RUNNING_SINCE=null
  [ "$code" -eq 0 ] || { STATE=error; return "$code"; }
  LAST_BACKUP_SECONDS=$(($(date +%s) - started))
  log "Respaldo base $kind: listo en ${LAST_BACKUP_SECONDS} s"
  refresh_info || return 1
  # Un completo que empezó después del último descarte vuelve a dar recuperación continua: el aviso se archiva.
  if [ "$kind" = full ] && [ -f "$DROPPED" ]; then
    last_drop=$(jq -r '.last // 0' "$DROPPED" 2>/dev/null || echo 0)
    [ "$started" -le "$last_drop" ] || mv -f "$DROPPED" "$DIR/wal-dropped.resolved.json"
  fi
}

STANZA_READY=0
round() {
  if ! pg_isready -q -h "$SOCKET" -U "${POSTGRES_USER:-timeclock}" -d "$STANZA"; then
    failed "PostgreSQL no acepta conexiones por $SOCKET (¿el servicio db está arriba?)"
    STATE=waiting
    return 2
  fi
  if [ "$STANZA_READY" != 1 ]; then
    # Idempotente: la crea si falta y, si ya existe, verifica que es de esta base y que la llave la abre. `check`
    # además fuerza un cambio de segmento y espera a verlo en el bucket: prueba el archive_command de punta a punta.
    run stanza-create || return 1
    run check || return 1
    STANZA_READY=1
  fi
  refresh_info || return 1
  kind=$(due)
  if [ "$kind" != none ]; then
    backup "$kind" || return 1
  fi
  STATE=ok LAST_ERROR="" LAST_ERROR_AT=null
}

scheduler() {
  if ! enabled; then
    STATE=disabled
    save_status
    log "PITR apagado (PITR_ENABLED=false): este servicio no hace nada hasta encenderlo"
    while :; do
      sleep 3600 &
      wait $!
    done
  fi
  config
  # docker stop: se detiene también el respaldo en curso (pgBackRest lo deja reanudable y suelta su candado).
  trap 'trap - TERM INT; kill -TERM 0 2>/dev/null || true; exit 143' TERM INT
  log "Servicio pitr: stanza $STANZA en gs://$(bucket)/$(prefix)/pitr (cifrado aes-256-cbc), revisa cada ${PITR_CHECK_SECONDS:-60} s"
  retry_at=0
  while :; do
    now=$(date +%s)
    if [ "$now" -ge "$retry_at" ]; then
      # Una falla espera PITR_RETRY_MINUTES (sin martillar al bucket); esperar a PostgreSQL (2) vuelve a revisar ya.
      if round; then code=0; else code=$?; fi
      [ "$code" -eq 0 ] || [ "$code" -eq 2 ] || retry_at=$((now + ${PITR_RETRY_MINUTES:-15} * 60))
    fi
    save_status
    sleep "${PITR_CHECK_SECONDS:-60}" &
    wait $!
  done
}

manual_backup() {
  kind="${1:-full}"
  case "$kind" in full | diff | incr) ;; *) fail "Tipo de respaldo desconocido: $kind (full | diff | incr)" ;; esac
  [ -f "$CONF" ] || config
  backup "$kind"
  save_status
}

restore() {
  target=""
  archive_off=""
  delta=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --target) target="${2:?--target necesita un instante: \"AAAA-MM-DD HH:MM:SS+00\"}"; shift 2 ;;
      --archive-off) archive_off=1; shift ;;
      --delta) delta=1; shift ;;
      *) fail "Opción desconocida: $1" ;;
    esac
  done
  [ "$(id -u)" != 0 ] || fail "Se restaura como el usuario postgres (docker compose run -u postgres ...)"
  config
  # Sin --delta solo en un volumen VACÍO (la copia para revisar, o una instalación nueva). --delta reescribe la base
  # actual en su lugar (solo lo que cambió, por checksums): con PostgreSQL detenido y tras una copia del volumen.
  if [ -n "$delta" ]; then
    ! pg_isready -q -h "$SOCKET" -d "$STANZA" -U "${POSTGRES_USER:-timeclock}" || fail "PostgreSQL sigue arriba: detén el servicio db"
  else
    [ -z "$(ls -A "$PGDATA" 2>/dev/null)" ] || fail "PGDATA ($PGDATA) no está vacío: restaura en un volumen nuevo o usa --delta"
  fi
  set -- --stanza="$STANZA"
  [ -z "$delta" ] || set -- "$@" --delta
  [ -z "$target" ] || set -- "$@" --type=time "--target=$target" --target-action=promote
  # Una restauración de PRUEBA nunca escribe en el repositorio de producción (archive_mode=off en la copia).
  [ -z "$archive_off" ] || set -- "$@" --archive-mode=off
  exec pgbackrest "$@" restore
}

command="${1:-}"
[ $# -eq 0 ] || shift
case "$command" in
  enabled) enabled ;;
  config) config ;;
  archive-timeout) printf '%ss\n' "${PITR_ARCHIVE_TIMEOUT_SECONDS:-60}" ;;
  archive) archive "$@" ;;
  scheduler) scheduler ;;
  backup) manual_backup "$@" ;;
  info) [ -f "$CONF" ] || config; exec pgbackrest --stanza="$STANZA" "$@" info ;;
  restore) restore "$@" ;;
  *) fail "Uso: timeclock-pgbackrest enabled|config|archive-timeout|archive <WAL>|scheduler|backup [full|diff]|info|restore" ;;
esac
