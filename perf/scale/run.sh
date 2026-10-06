#!/bin/sh
# Escalado horizontal de punta a punta en un entorno AISLADO (perf/scale/docker-compose.scale.yml): la MISMA
# topología de docker-compose.yml (gateway Nginx → N réplicas → PgBouncer → PostgreSQL, migración única) con
# su propio proyecto (timeclock-scale), secretos de un solo uso y base en memoria. Nunca toca el entorno de
# trabajo (ni sus contenedores, ni su base, ni su .env).
#
#   perf/scale/run.sh                                    → todo (≈10 min)
#   SCALE_REPLICAS="1 3" SCALE_RATES="300 600 1200" PERF_DURATION=20s perf/scale/run.sh
#   SCALE_CPUS=3 SCALE_API_WORKERS=2 perf/scale/run.sh   → recursos de cada réplica (CPU y procesos)
#   SCALE_KEEP=1 perf/scale/run.sh                       → deja el entorno arriba al terminar
#
# Qué demuestra (resultados en perf/results/scale/, resumen en report.md):
#   1. Sin carrera de migraciones: 3 réplicas que migran a la vez sobre una base vacía (una migra, las demás
#      esperan su candado y no encuentran nada).
#   2. Carga con 1 y con N réplicas (k6 por el gateway): lo logrado, errores, latencia, reparto por réplica
#      (log del gateway) y conexiones reales a PostgreSQL contra su presupuesto (PgBouncer).
#   3. Canal WebSocket por el gateway, repartido entre réplicas.
#   4. Mantenimiento: una sola instancia a la vez, y el candado se suelta si el proceso muere.
#   5. Una réplica que se apaga (docker stop) y otra que se cae (docker kill) en plena carga: 0 errores
#      tras los reintentos de la app.
#   6. Despliegue en plena carga: `docker compose up -d` (recrea TODAS las réplicas a la vez) contra
#      scripts/deploy.sh (primero las nuevas, luego se retiran las viejas una por una).
set -eu
PERF="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$PERF/../.." && pwd)"
OUT="$PERF/results/scale"
REPLICAS="${SCALE_REPLICAS:-1 3}"
RATES="${SCALE_RATES:-300 600 1200}"
DURATION="${PERF_DURATION:-20s}"
export BACKEND_CPUS="${SCALE_CPUS:-3}" SCALE_API_WORKERS="${SCALE_API_WORKERS:-2}"
mkdir -p "$OUT"
rm -f "$OUT"/*
PERF_ENV_FILE=$(mktemp)
export PERF_ENV_FILE

dc() { docker compose -f "$ROOT/docker-compose.yml" -f "$PERF/scale/docker-compose.scale.yml" "$@"; }
cleanup() {
  [ "${SCALE_KEEP:-0}" = 1 ] || dc --profile load down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$PERF_ENV_FILE"
}
trap cleanup EXIT
psql_db() { dc exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$0"' "$1"; }
# PgBouncer atiende al usuario de la API (DB_APP_USER, el de sus estadísticas); sin él, al dueño (instalación anterior).
pools() { dc exec -T pgbouncer sh -c 'PGPASSWORD="${DB_APP_PASSWORD:-$POSTGRES_PASSWORD}" psql -h 127.0.0.1 -p 6432 -U "${DB_APP_USER:-$POSTGRES_USER}" -d pgbouncer -A -c "SHOW POOLS"'; }
healthy() { dc ps --format '{{.Service}} {{.Health}}' backend | grep -c ' healthy$' || true; }
wait_replicas() {
  for _ in $(seq 1 120); do [ "$(healthy)" -ge "$1" ] && return 0; sleep 2; done
  echo "Las réplicas no quedaron listas" >&2; exit 1
}
wait_gateway() {
  for _ in $(seq 1 60); do curl -fs http://127.0.0.1:18080/api/health/ready >/dev/null 2>&1 && return 0; sleep 2; done
  echo "El gateway no responde" >&2; exit 1
}
# IP → nombre de cada réplica (para leer el reparto en el log del gateway).
replica_map() {
  for id in $(dc ps -q backend); do docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}} {{.Name}}' "$id"; done
}
# Conexiones cada segundo: reales a PostgreSQL y el pool de PgBouncer (hasta que aparezca "$1.stop").
sample() {
  while [ ! -f "$1.stop" ]; do
    printf 'pg=%s\n' "$(psql_db "select count(*) from pg_stat_activity where backend_type = 'client backend'")" >> "$1"
    pools | grep -E '^(database|timeclock)\|' >> "$1" || true
    sleep 1
  done
}
k6() { dc --profile load run --rm --no-deps "$@"; }

# Primero la imagen y después los secretos: el generador de la imagen recién construida conoce todas las variables
# (con una imagen vieja faltarían las nuevas, p. ej. el usuario de la API, y la prueba no sería la de producción).
dc build -q db backend migrate pgbouncer frontend
docker run --rm --entrypoint python timeclock-scale-backend scripts/generate_secrets.py > "$PERF_ENV_FILE"
dc --profile load down -v --remove-orphans >/dev/null 2>&1 || true  # siempre desde una base vacía

echo "== 1. Carrera de migraciones: 3 réplicas migran a la vez sobre una base vacía =="
dc up -d --wait db pgbouncer >/dev/null
SCALE_RUN_MIGRATIONS=1 SCALE_OWNER_PASSWORD="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$PERF_ENV_FILE")" \
  dc up -d --no-deps --scale backend=3 backend >/dev/null
wait_replicas 3
{
  for name in $(dc ps --format '{{.Name}}' backend); do
    printf '%s: migraciones aplicadas=%s, esperó su turno=%s\n' "$name" \
      "$(docker logs "$name" 2>&1 | grep -c 'Running upgrade' || true)" \
      "$(docker logs "$name" 2>&1 | grep -c 'Otra réplica está migrando' || true)"
  done
  printf 'alembic_version: %s\n' "$(psql_db "select string_agg(version_num, ',') from alembic_version")"
} | tee "$OUT/migration-race.txt"

echo "== Entorno completo (migración única con el servicio migrate) =="
dc up -d --scale backend=1 >/dev/null
wait_replicas 1
wait_gateway
dc exec -T --index 1 backend python - "${PERF_EMPLOYEES:-500}" < "$PERF/seed.py"
printf 'migrate: %s\n' "$(dc logs migrate 2>&1 | grep -c 'Running upgrade' || true) migraciones nuevas (ya estaba al día)" \
  | tee -a "$OUT/migration-race.txt"

echo "== 2. Carga por el gateway =="
for n in $REPLICAS; do
  dc up -d --no-recreate --scale backend="$n" >/dev/null
  wait_replicas "$n"
  sleep 8  # el gateway vuelve a resolver el DNS (cada 5 s) y ve a todas las réplicas
  replica_map > "$OUT/replicas-$n.txt"
  for rate in $RATES; do
    stage="load-$n-$rate"
    echo "-- $n réplica(s), $rate pet/s durante $DURATION"
    since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    sample "$OUT/$stage.conns" &
    k6 -e RATE="$rate" -e DURATION="$DURATION" -e BASE_URL=http://frontend:80 k6 run --quiet \
      --summary-export "/perf/results/scale/$stage.json" /perf/load.js > "$OUT/$stage.log" 2>&1 || true
    touch "$OUT/$stage.conns.stop"
    sleep 7  # el log del gateway se escribe en bloques cada 5 s
    dc logs --no-log-prefix --since "$since" frontend 2>/dev/null | grep -o 'up=[0-9.:, ]*' | sed 's/^up=//' > "$OUT/$stage.up" || true
    docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' $(dc ps -q backend) > "$OUT/$stage.stats"
    sleep 3
  done
done

rm -f "$OUT"/*.stop

echo "== 3. Canal WebSocket por el gateway =="
since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker run --rm --network timeclock-scale_default -v "$PERF/scale:/scale:ro" --entrypoint python \
  timeclock-scale-backend /scale/ws_check.py 60 | tee "$OUT/ws.json"
sleep 7
dc logs --no-log-prefix --since "$since" frontend 2>/dev/null | grep '/api/ws/validation' | grep -o 'up=[0-9.:]*' | sed 's/^up=//' > "$OUT/ws.up" || true

echo "== 4. Mantenimiento: una instancia a la vez =="
holder='import socket, sys, time
from app.services.maintenance_service import exclusive
host = socket.gethostname()
result = exclusive(lambda db: print(host, "tomó el candado", flush=True) or time.sleep(float(sys.argv[1])) or "trabajó")
print(host, result or "se saltó la vuelta (otra instancia tiene el candado)")'
{
  echo "Las réplicas intentan la vuelta a la vez (cada una sostiene el candado 4 s si lo obtiene):"
  for i in $(seq 1 "$(dc ps -q backend | wc -l)"); do dc exec -T --index "$i" backend python -c "$holder" 4 & done
  wait
  echo "Un proceso con el candado muere a la mitad (kill -9): ¿queda pegado en PgBouncer?"
  dc exec -T --index 1 backend sh -c "python -c '$holder' 60 & sleep 3; kill -9 \$!" || true
  sleep 1
  dc exec -T --index 2 backend python -c "$holder" 0
  echo "Vueltas programadas (MAINTENANCE_INTERVAL_SECONDS=5) en el log de cada réplica:"
  dc logs backend 2>&1 | grep -c "Falló el mantenimiento" | sed 's/^/  fallas del mantenimiento: /'
} 2>&1 | tee "$OUT/maintenance.txt"

echo "== 5. Una réplica se apaga y otra se cae en plena carga =="
n=$(dc ps -q backend | wc -l | tr -d ' ')
replica_map > "$OUT/replicas-failover.txt"
since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
k6 -e RATE="${SCALE_FAILOVER_RATE:-200}" -e DURATION=45s -e BASE_URL=http://frontend:80 k6 run --quiet \
  --summary-export /perf/results/scale/failover.json /perf/scale/failover.js > "$OUT/failover.log" 2>&1 &
K6=$!
sleep 12
victim=$(dc ps --format '{{.Name}}' backend | sed -n "${n}p")
echo "$(date -u +%T) docker stop $victim (apagado ordenado)" | tee "$OUT/failover-events.txt"
docker stop "$victim" >/dev/null
sleep 10
victim=$(dc ps --format '{{.Name}}' backend | sed -n '1p')
echo "$(date -u +%T) docker kill $victim (caída abrupta)" | tee -a "$OUT/failover-events.txt"
docker kill "$victim" >/dev/null
wait "$K6" || true
sleep 7
dc logs --no-log-prefix --since "$since" frontend 2>/dev/null | grep -o 'up=[0-9.:, ]*' | sed 's/^up=//' > "$OUT/failover.up" || true
dc logs --no-log-prefix --since "$since" frontend 2>/dev/null | grep -oE '" (5[0-9]{2}) ' | sort | uniq -c > "$OUT/failover-gateway-5xx.txt" || true
dc up -d --scale backend="$n" >/dev/null  # vuelve a N réplicas
wait_replicas "$n"
sleep 8

echo "== 6. Despliegue en plena carga: recrear todo a la vez contra scripts/deploy.sh =="
# $1 = nombre de la medición; $2... = el comando de despliegue (con el tráfico de failover.js corriendo).
deploy_under_load() {
  name=$1
  shift
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  k6 -e RATE="${SCALE_FAILOVER_RATE:-200}" -e DURATION=75s -e BASE_URL=http://frontend:80 k6 run --quiet \
    --summary-export "/perf/results/scale/$name.json" /perf/scale/failover.js > "$OUT/$name.log" 2>&1 &
  K6=$!
  sleep 10
  echo "$(date -u +%T) $*" | tee "$OUT/$name-events.txt"
  "$@" > "$OUT/$name-deploy.log" 2>&1
  echo "$(date -u +%T) terminó: $(healthy) réplicas sanas" | tee -a "$OUT/$name-events.txt"
  wait "$K6" || true
  sleep 7
  dc logs --no-log-prefix --since "$since" frontend 2>/dev/null | grep -oE '" (5[0-9]{2}) ' | sort | uniq -c \
    > "$OUT/$name-gateway-5xx.txt" || true
}
deploy_under_load redeploy dc up -d --force-recreate --no-deps --scale backend="$n" backend
wait_replicas "$n"
sleep 8
deploy_under_load rolling env COMPOSE_FILE="$ROOT/docker-compose.yml:$PERF/scale/docker-compose.scale.yml" \
  "$ROOT/scripts/deploy.sh"

# El resumen corre en la imagen de la API (Python 3.14): no depende del Python del equipo.
docker run --rm -v "$OUT:/out" -v "$PERF/scale:/scale:ro" --entrypoint python timeclock-scale-backend \
  /scale/report.py /out "$REPLICAS" "$RATES"
