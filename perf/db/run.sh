#!/bin/sh
# Auditoría de la BASE con volumen real, en un PostgreSQL 16 AISLADO (jamás la base de trabajo):
# aplica las migraciones, siembra el volumen (perf/db/seed.sql, ≈10 min la primera vez) y hace
# EXPLAIN (ANALYZE, BUFFERS) de cada consulta de los repositorios (perf/db/explain.py).
#
#   perf/db/run.sh                    → todos los casos
#   perf/db/run.sh attendance         → solo los casos cuyo nombre contiene "attendance"
#   PERF_DB_KEEP=1 perf/db/run.sh     → deja la base sembrada arriba: la siguiente corrida solo aplica las
#                                        migraciones nuevas y vuelve a medir (antes/después de un cambio).
#                                        psql: docker exec -it timeclock-perf-db psql -U perf volume
#   docker rm -f timeclock-perf-db    → la borra (sus datos viven en memoria del contenedor)
#
# Resultado: perf/results/db-plans.txt (resumen por tiempo con banderas y el plan de cada sentencia).
# Requiere la imagen dev del backend (scripts/quality.sh la construye) y Docker.
set -eu
cd "$(dirname "$0")/../.."
NAME=timeclock-perf-db
NET=timeclock-perf-db
IMAGE="${PERF_DB_IMAGE:-time-clock-backend-dev}"
docker network create "$NET" >/dev/null 2>&1 || true
cleanup() {
  if [ "${PERF_DB_KEEP:-0}" != 1 ]; then
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

FRESH=0
if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  # Sin puerto publicado ni volumen: la base solo existe dentro de esta red de Docker.
  # fsync apagado solo acelera la siembra; no cambia los planes que se miden.
  docker run -d --rm --name "$NAME" --network "$NET" --shm-size=1g \
    -e POSTGRES_USER=perf -e POSTGRES_PASSWORD=perf -e POSTGRES_DB=volume postgres:16-alpine \
    postgres -c shared_buffers=256MB -c max_connections=150 -c maintenance_work_mem=512MB -c max_wal_size=4GB \
    -c fsync=off -c synchronous_commit=off -c full_page_writes=off >/dev/null
  until docker exec "$NAME" pg_isready -U perf -d volume >/dev/null 2>&1; do sleep 1; done
  FRESH=1
fi

KEY=$(docker run --rm --entrypoint python "$IMAGE" -c \
  "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
in_backend() {
  docker run --rm --network "$NET" -u "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONPATH=/app \
    -e DATABASE_URL="postgresql+psycopg://perf:perf@$NAME:5432/volume" -e DATA_ENCRYPTION_KEY="$KEY" \
    -e FIRST_ADMIN_EMAIL= -e FIRST_COMPANY_EMAIL= -e MAINTENANCE_INTERVAL_SECONDS=0 \
    -v "$PWD:/app" "$IMAGE" "$@"
}

in_backend alembic upgrade head
if [ "$FRESH" = 1 ]; then
  echo "Sembrando el volumen (la primera vez tarda unos minutos)..."
  docker exec -i "$NAME" psql -q -v ON_ERROR_STOP=1 -U perf -d volume < perf/db/seed.sql >/dev/null
fi
# Estadísticas y mapa de visibilidad al día (como en producción con autovacuum).
docker exec "$NAME" psql -q -U perf -d volume -c "VACUUM (ANALYZE)"
mkdir -p perf/results
in_backend python perf/db/explain.py perf/results/db-plans.txt "${1:-}"
