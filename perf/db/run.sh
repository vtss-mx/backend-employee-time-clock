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
# Resultado: perf/results/db-plans.txt (resumen por tiempo con banderas y el plan de cada sentencia). Una corrida
# con filtro escribe perf/results/db-plans-filtered.txt: nunca reemplaza la auditoría completa con unos cuantos casos.
# Requiere la imagen dev del backend (scripts/quality.sh la construye) y Docker.
#
# Se mide COMO EN PRODUCCIÓN: las migraciones y la siembra con el dueño; los planes con el usuario de la API
# (sujeto a la seguridad por fila) y cada caso con su alcance (la empresa o la plataforma, perf/db/explain.py).
#   PERF_DB_ROLE=owner perf/db/run.sh  → los planes con el dueño (superusuario: sin seguridad por fila), para
#                                        comparar cuánto cuesta la política.
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
  # Sin puerto publicado ni volumen: la base solo existe dentro de esta red de Docker. Con los MISMOS parámetros
  # que producción (postgres/start.sh: memoria, SSD, autovacuum, jit=off...); fsync apagado solo acelera la
  # siembra y no cambia los planes que se miden.
  docker run -d --rm --name "$NAME" --network "$NET" --shm-size=1g \
    -e POSTGRES_USER=perf -e POSTGRES_PASSWORD=perf -e POSTGRES_DB=volume \
    -v "$PWD/postgres/start.sh:/usr/local/bin/timeclock-postgres.sh:ro" --entrypoint /usr/local/bin/timeclock-postgres.sh \
    postgres:16-alpine -c fsync=off -c synchronous_commit=off -c full_page_writes=off >/dev/null
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

STARTED=$(date +%s)
in_backend alembic upgrade head
echo "Migraciones aplicadas en $(( $(date +%s) - STARTED )) s"
# Roles de mínimo privilegio (los mismos que crea el servicio migrate): el usuario de la API mide los planes.
APP_PASSWORD=perf-app
in_backend env DB_APP_PASSWORD="$APP_PASSWORD" DB_READONLY_PASSWORD= python -m app.cli db roles
if [ "$FRESH" = 1 ]; then
  echo "Sembrando el volumen (la primera vez tarda unos minutos)..."
  docker exec -i "$NAME" psql -q -v ON_ERROR_STOP=1 -U perf -d volume < perf/db/seed.sql >/dev/null
fi
# Estadísticas y mapa de visibilidad al día (como en producción con autovacuum).
docker exec "$NAME" psql -q -U perf -d volume -c "VACUUM (ANALYZE)"
mkdir -p perf/results
PLANS=perf/results/db-plans.txt
[ -z "${1:-}" ] || PLANS=perf/results/db-plans-filtered.txt
if [ "${PERF_DB_ROLE:-app}" = owner ]; then
  in_backend python perf/db/explain.py "$PLANS" "${1:-}"
else
  in_backend env DATABASE_URL="postgresql+psycopg://timeclock_app:$APP_PASSWORD@$NAME:5432/volume" \
    DATABASE_DIRECT_URL="postgresql+psycopg://perf:perf@$NAME:5432/volume" DB_APP_PASSWORD="$APP_PASSWORD" \
    python perf/db/explain.py "$PLANS" "${1:-}"
fi
