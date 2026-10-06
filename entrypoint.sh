#!/bin/sh
# Arranque del contenedor de la API.
#   ./entrypoint.sh           → (migraciones si RUN_MIGRATIONS=1) + API
#   ./entrypoint.sh migrate   → SOLO migraciones y termina (servicio único `migrate` de docker compose)
set -e

migrate() {
  # Con reintentos: la BD puede tardar en aceptar conexiones tras un reinicio. Van por la conexión directa
  # a PostgreSQL (DATABASE_DIRECT_URL, nunca PgBouncer) y con candado: si varias réplicas migran a la vez,
  # solo una trabaja (alembic/env.py).
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
  # Roles de mínimo privilegio y sus permisos (app/core/db_roles.py), con el dueño y en cada despliegue: crea lo
  # que falta, rota contraseñas y da permisos a las tablas nuevas. Idempotente.
  python -m app.cli db roles
}

if [ "${1:-}" = "migrate" ]; then
  migrate
  exit 0
fi
# Un solo contenedor (instalación simple, desarrollo): migra al arrancar. Con varias réplicas docker compose
# pone RUN_MIGRATIONS=0 y migra una sola vez con el servicio `migrate` ANTES de arrancar la API: N réplicas
# que arrancan juntas no compiten por migrar ni arrancan con la estructura a medias.
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  migrate
fi

# Varios procesos: Python ejecuta un hilo de código a la vez por proceso (GIL); con N procesos
# la API atiende ~N veces más peticiones. El estado compartido (retos de prueba de vida) vive
# en PostgreSQL.
API_WORKERS="${API_WORKERS:-0}"
if [ "$API_WORKERS" -le 0 ]; then
  API_WORKERS=$(python -c "from app.core.config import settings; print(settings.API_WORKERS)")
fi
export API_WORKERS

# De quién se acepta X-Forwarded-For (la IP real del cliente): solo del proxy. `auto` = las redes a las que
# está conectado este contenedor (la red de docker compose, donde vive el gateway Nginx); el backend no
# publica puertos, así que nadie más lo alcanza. Detrás de otro balanceador, su IP o su red (CIDR).
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-auto}"
if [ "$FORWARDED_ALLOW_IPS" = "auto" ]; then
  FORWARDED_ALLOW_IPS=$(python -c "from app.core.system import trusted_proxies; print(trusted_proxies())")
fi

echo "Iniciando API con $API_WORKERS procesos (proxies de confianza: $FORWARDED_ALLOW_IPS)..."
# Apagado ordenado: al recibir SIGTERM cada proceso drena SHUTDOWN_DRAIN_SECONDS (app/core/lifecycle.py) y
# luego termina lo que lleva hasta SHUTDOWN_GRACE_SECONDS. El orquestador debe esperar más que la suma
# (docker compose: stop_grace_period).
# Cada valor por defecto de este script es el mismo de backend-employee-time-clock/.env y de
# scripts/generate_secrets.py (RUNTIME); tests/test_env_files.py lo verifica.
# Keep-alive MAYOR que el keepalive_timeout del gateway hacia la API (4 s): cierra primero el proxy.
exec uvicorn app.main:app \
  --workers "$API_WORKERS" \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips="$FORWARDED_ALLOW_IPS" \
  --timeout-keep-alive "${UVICORN_TIMEOUT_KEEP_ALIVE:-5}" \
  --timeout-graceful-shutdown "${SHUTDOWN_GRACE_SECONDS:-20}" \
  --limit-concurrency "${UVICORN_LIMIT_CONCURRENCY:-4000}" \
  --backlog "${UVICORN_BACKLOG:-4096}" \
  --ws-max-size "${UVICORN_WS_MAX_SIZE:-65536}"
