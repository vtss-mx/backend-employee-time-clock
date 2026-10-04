#!/bin/sh
# Pruebas de carga por etapas en un entorno AISLADO (base en memoria, secretos de un solo uso).
#   perf/run.sh                       → 1 000, 2 000, 3 000 … hasta 1 000 000 peticiones por segundo
#   perf/run.sh 1000 5000             → solo esas etapas
#   PERF_DURATION=30s perf/run.sh     → más tiempo por etapa
#   PERF_OVERRIDE=/ruta/override.yml  → mismas etapas con otra configuración (comparar antes/después)
# Resultados: perf/results/report.md (tabla) y un JSON de k6 por etapa.
set -eu
cd "$(dirname "$0")"
RATES="${*:-1000 2000 3000 5000 10000 20000 50000 100000 250000 500000 1000000}"
DURATION="${PERF_DURATION:-20s}"
# PERF_OVERRIDE=archivo.yml agrega un compose con variables a comparar (p. ej. otro límite de admisión).
COMPOSE="docker compose -f docker-compose.perf.yml${PERF_OVERRIDE:+ -f $PERF_OVERRIDE}"
mkdir -p results
PERF_ENV_FILE=$(mktemp)
export PERF_ENV_FILE
trap '$COMPOSE down -v >/dev/null 2>&1; rm -f "$PERF_ENV_FILE"' EXIT

docker run --rm --entrypoint python time-clock-backend scripts/generate_secrets.py > "$PERF_ENV_FILE"
$COMPOSE up -d db backend
printf 'Esperando a la API'
for _ in $(seq 1 90); do
  curl -fs http://127.0.0.1:8100/api/health/live >/dev/null 2>&1 && break
  printf '.'
  sleep 2
done
echo
$COMPOSE exec -T backend python - "${PERF_EMPLOYEES:-500}" < seed.py
# Sin sesión no hay prueba: se verifica antes de medir (una prueba que solo falla no es un resultado).
LOGIN=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
  -d '{"email":"carga@carga-timeclock.com","password":"Carga12345"}' http://127.0.0.1:8100/api/auth/login)
if [ "$LOGIN" != "200" ]; then
  echo "No se pudo iniciar sesión en el entorno de prueba (HTTP $LOGIN): se cancela la prueba" >&2
  exit 1
fi

for rate in $RATES; do
  echo "== Etapa: $rate peticiones por segundo durante $DURATION =="
  $COMPOSE run --rm --no-deps -e RATE="$rate" -e DURATION="$DURATION" k6 run --quiet \
    --summary-export "/perf/results/rate-$rate.json" /perf/load.js > "results/rate-$rate.log" 2>&1 || true
  sleep 5
done
# Capacidad adaptativa al final (límite vigente, descartes, APIs con más demanda): la ve solo el ADMIN.
ADMIN_TOKEN=$(curl -s -H 'Content-Type: application/json' \
  -d '{"email":"admin@carga-timeclock.com","password":"Admin123456"}' http://127.0.0.1:8100/api/auth/login |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["data"]["access_token"])' 2>/dev/null || true)
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8100/api/admin/errors/server \
  > results/server.json || true
python3 report.py results $RATES
# Cada etapa debe haber medido algo: si alguna no envió peticiones, el reporte no es válido.
grep -l "script exception" results/rate-*.log >/dev/null 2>&1 && { echo "Alguna etapa falló (ver results/*.log)" >&2; exit 1; }
exit 0
