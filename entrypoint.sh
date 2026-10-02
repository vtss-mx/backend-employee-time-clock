#!/bin/sh
set -e

# Migraciones con reintentos: la BD puede tardar en aceptar conexiones tras un reinicio.
attempt=1
until alembic upgrade head; do
  if [ "$attempt" -ge "${MIGRATION_RETRIES:-20}" ]; then
    echo "No fue posible aplicar las migraciones tras $attempt intentos" >&2
    exit 1
  fi
  echo "BD no disponible para migraciones (intento $attempt). Reintentando en 3 s..."
  attempt=$((attempt + 1))
  sleep 3
done

# Varios procesos: Python ejecuta un hilo de código a la vez por proceso (GIL); con N procesos
# la API atiende ~N veces más peticiones. El estado compartido (retos de prueba de vida) vive
# en PostgreSQL.
API_WORKERS="${API_WORKERS:-0}"
if [ "$API_WORKERS" -le 0 ]; then
  API_WORKERS=$(python -c "from app.core.config import settings; print(settings.API_WORKERS)")
fi
export API_WORKERS

echo "Iniciando API con $API_WORKERS procesos..."
exec uvicorn app.main:app \
  --workers "$API_WORKERS" \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
  --timeout-keep-alive 5 \
  --timeout-graceful-shutdown 20 \
  --limit-concurrency "${UVICORN_LIMIT_CONCURRENCY:-4000}" \
  --backlog "${UVICORN_BACKLOG:-4096}" \
  --ws-max-size "${UVICORN_WS_MAX_SIZE:-65536}"
