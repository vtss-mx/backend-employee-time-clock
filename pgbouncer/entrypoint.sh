#!/bin/sh
# Arma la configuración de PgBouncer con las variables del backend y lo arranca (sin archivos con secretos
# en la imagen: la contraseña solo existe en el .env y, al correr, en /run/pgbouncer con permisos 600).
#
# Por qué modo TRANSACCIÓN: cada réplica de la API abre API_WORKERS x (DB_POOL_SIZE + DB_MAX_OVERFLOW)
# conexiones de cliente; PgBouncer las atiende con pocas conexiones reales a PostgreSQL, que cambian de cliente
# en cada transacción. Por eso la API no usa nada de sesión por esta vía (ni sentencias preparadas, ni SET, ni
# candados de sesión) y las migraciones van directo a PostgreSQL (README, "Escalar en cualquier momento").
#
# Presupuesto de conexiones (lo verifica quien escala):
#   réplicas x API_WORKERS x (DB_POOL_SIZE + DB_MAX_OVERFLOW)  <=  PGBOUNCER_MAX_CLIENT_CONN
#   PGBOUNCER_MAX_DB_CONNECTIONS                               <=  max_connections de PostgreSQL - reserva directa
set -eu
: "${POSTGRES_DB:?Falta POSTGRES_DB}"
# La API llega con su usuario de MÍNIMO PRIVILEGIO (DB_APP_USER: sin DDL, sujeto a la seguridad por fila; lo crea
# `python -m app.cli db roles` en el servicio migrate) y PgBouncer se conecta a PostgreSQL con ese mismo usuario.
# Sin su contraseña (una instalación anterior) se usa el dueño, como antes.
if [ -n "${DB_APP_PASSWORD-}" ]; then
  DB_USER="${DB_APP_USER-timeclock_app}" DB_PASSWORD="$DB_APP_PASSWORD"
else
  : "${POSTGRES_USER:?Falta POSTGRES_USER}" "${POSTGRES_PASSWORD:?Falta POSTGRES_PASSWORD}"
  DB_USER="$POSTGRES_USER" DB_PASSWORD="$POSTGRES_PASSWORD"
fi
DIR=/run/pgbouncer
umask 077

# Formato de auth_file: "usuario" "contraseña" (una comilla doble dentro del valor se escribe doble). Con la
# contraseña en claro PgBouncer autentica a la API y a sí mismo ante PostgreSQL con SCRAM-SHA-256.
quote() { printf '"%s"' "$(printf '%s' "$1" | sed 's/"/""/g')"; }
printf '%s %s\n' "$(quote "$DB_USER")" "$(quote "$DB_PASSWORD")" > "$DIR/userlist.txt"

cat > "$DIR/pgbouncer.ini" <<INI
[databases]
; statement_timeout lo fija PgBouncer en cada conexión al servidor (la API no puede mandarlo al conectar:
; PgBouncer rechaza ese parámetro). Mismo valor que DB_STATEMENT_TIMEOUT_MS del backend.
${POSTGRES_DB} = host=${PGBOUNCER_SERVER_HOST:-db} port=${PGBOUNCER_SERVER_PORT:-5432} dbname=${POSTGRES_DB} connect_query='SET statement_timeout = ${DB_STATEMENT_TIMEOUT_MS:-15000}'

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
unix_socket_dir =
auth_type = scram-sha-256
auth_file = ${DIR}/userlist.txt
; SHOW POOLS / SHOW STATS (base "pgbouncer") con las credenciales de la API, solo lectura.
stats_users = ${DB_USER}

pool_mode = transaction
max_client_conn = ${PGBOUNCER_MAX_CLIENT_CONN:-2000}
default_pool_size = ${PGBOUNCER_POOL_SIZE:-40}
min_pool_size = ${PGBOUNCER_MIN_POOL_SIZE:-5}
reserve_pool_size = ${PGBOUNCER_RESERVE_POOL_SIZE:-10}
reserve_pool_timeout = ${PGBOUNCER_RESERVE_POOL_TIMEOUT:-3}
max_db_connections = ${PGBOUNCER_MAX_DB_CONNECTIONS:-60}

; Toda espera con tiempo límite (regla 7): conectar al servidor, esperar una conexión libre del pool y una
; transacción abierta sin actividad (una vuelta del mantenimiento sostiene la suya mientras trabaja).
; Cada valor por defecto es el mismo de backend-employee-time-clock/.env y de scripts/generate_secrets.py
; (RUNTIME); tests/test_env_files.py lo verifica.
server_connect_timeout = ${DB_CONNECT_TIMEOUT_SECONDS:-5}
server_login_retry = 2
query_wait_timeout = ${PGBOUNCER_QUERY_WAIT_TIMEOUT:-10}
client_login_timeout = ${PGBOUNCER_CLIENT_LOGIN_TIMEOUT:-15}
idle_transaction_timeout = ${PGBOUNCER_IDLE_TRANSACTION_TIMEOUT:-1800}
server_idle_timeout = ${PGBOUNCER_SERVER_IDLE_TIMEOUT:-600}
server_lifetime = ${PGBOUNCER_SERVER_LIFETIME:-3600}

; Herramientas como DBeaver mandan extra_float_digits al conectar; la API no manda parámetros de sesión.
ignore_startup_parameters = extra_float_digits
log_connections = 0
log_disconnections = 0
log_pooler_errors = 1
stats_period = 60
INI

exec pgbouncer "$DIR/pgbouncer.ini"
