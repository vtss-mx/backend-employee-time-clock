# PgBouncer en modo transacción entre las réplicas de la API y PostgreSQL (README, "Escalar en cualquier
# momento"). Imagen propia y mínima: el paquete oficial de Alpine (PgBouncer 1.25) y la configuración se arma
# al arrancar con las MISMAS variables del backend (.env): credenciales POSTGRES_* y DB_STATEMENT_TIMEOUT_MS.
FROM alpine:3.24

# postgresql-client: pg_isready (healthcheck) y psql para mirar el pool (SHOW POOLS) en producción.
RUN apk add --no-cache "pgbouncer~=1.25" postgresql-client \
    && adduser -S -D -H pgbouncer \
    && install -d -o pgbouncer -m 700 /run/pgbouncer

COPY --chmod=755 entrypoint.sh /usr/local/bin/pgbouncer-entrypoint.sh
USER pgbouncer
EXPOSE 6432
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD pg_isready -q -h 127.0.0.1 -p 6432 -U "$POSTGRES_USER" -d "$POSTGRES_DB" || exit 1
ENTRYPOINT ["/usr/local/bin/pgbouncer-entrypoint.sh"]
