# Versión mayor de PostgreSQL: la del servidor (etapa `db`) y la del cliente de los respaldos (etapa `backup`) son la
# MISMA (un pg_restore más nuevo deja sentencias que el servidor viejo no conoce). Subirla es cambiarla aquí.
ARG PG_MAJOR=16

FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FACE_MODELS_DIR=/app/models

WORKDIR /app

# libglib2.0-0 es requerido por OpenCV headless en imágenes slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/facial_recognition/model_store.py /tmp/model_store.py
# Descarga y verifica (SHA-256) los modelos ONNX durante el build.
RUN python /tmp/model_store.py /app/models && rm /tmp/model_store.py

COPY . .

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=4).status == 200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]

# ---------- Respaldos de la base (servicio `backup` de docker compose) ----------
#   La imagen de la API más pg_dump / pg_restore. `python -m app.cli db backup --loop` (app/services/db_backup.py).
#   El cliente es de la MISMA versión mayor que el servidor (`db`: postgres:16), del repositorio oficial de
#   PostgreSQL (PGDG): el de Debian (17) escribe `SET transaction_timeout`, que PostgreSQL 16 no conoce, y la
#   restauración falla (comprobado con scripts/db_restore_check.sh). Al subir de versión el servidor se cambia
#   PG_MAJOR aquí (o con `--build-arg`) junto con la imagen de `db`.
FROM runtime AS backup
ARG PG_MAJOR
USER root
ADD --chmod=644 https://www.postgresql.org/media/keys/ACCC4CF8.asc /usr/share/keyrings/pgdg.asc
RUN . /etc/os-release \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-${PG_MAJOR} \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /backups && chown appuser:appuser /backups
# /backups existe en la imagen y es de appuser: docker crea el volumen `backups` copiando ese dueño. Sin esto el
# volumen nace de root y pg_dump no puede escribir ("Permission denied" en el primer arranque de una instalación).
USER appuser
HEALTHCHECK NONE
ENTRYPOINT ["python", "-m", "app.cli", "db", "backup", "--loop"]

# ---------- PostgreSQL de la plataforma: servicios `db` y `pitr` (README "Respaldos y restauración") ----------
#   docker build --target db -t time-clock-db backend-employee-time-clock
#   La imagen oficial postgres:16-alpine más pgBackRest (archivo continuo del WAL y respaldos base al bucket, cifrados:
#   recuperación a un punto en el tiempo) y sus scripts (postgres/start.sh y postgres/pgbackrest.sh).
#   pgBackRest se compila de su tarball de distribución, FIJO y verificado por SHA-256 (el que publica el proyecto
#   junto al tarball): Alpine 3.24 trae 2.58.0, y 2.59 corrige justo lo que más importa aquí: `archive-push-queue-max`
#   no se respetaba cuando un WAL fallaba (el guardián del disco) y subllaves/sales de cifrado débiles (2.59.3).
#   Para subir de versión: PGBACKREST_VERSION y PGBACKREST_SHA256 juntos, y scripts/db_pitr_check.sh antes de usarla.
FROM postgres:${PG_MAJOR}-alpine AS pgbackrest-build
ARG PGBACKREST_VERSION=2.59.3
ARG PGBACKREST_SHA256=14037901db002e5536a948bf9f0fc0ff6cde31f4e675d3e9b46f129071bf2e5f
ADD --checksum=sha256:${PGBACKREST_SHA256} \
    https://github.com/pgbackrest/pgbackrest/releases/download/release/${PGBACKREST_VERSION}/pgbackrest-${PGBACKREST_VERSION}.tar.gz \
    /tmp/pgbackrest.tar.gz
# Sin SFTP ni systemd (no se usan): menos dependencias. libpq es la de la propia imagen de PostgreSQL.
RUN apk add --no-cache build-base meson pkgconf openssl-dev libxml2-dev lz4-dev zstd-dev bzip2-dev zlib-dev \
    && tar -xzf /tmp/pgbackrest.tar.gz -C /tmp \
    && cd /tmp/pgbackrest-${PGBACKREST_VERSION} \
    && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig meson setup build . --buildtype=release \
       -Dlibssh2=disabled -Dlibsystemd=disabled \
    && ninja -C build \
    && strip build/src/pgbackrest \
    && install -D -m 755 build/src/pgbackrest /out/pgbackrest

FROM postgres:${PG_MAJOR}-alpine AS db
# Bibliotecas de pgBackRest y jq (el servicio pitr lee `pgbackrest info --output=json`). /etc/pgbackrest: la
# configuración que se escribe al arrancar (600, con la llave del repositorio). /var/lib/timeclock-pitr: el volumen
# `pitr` (cola del archivo asíncrono, estado del servicio pitr y aviso de WAL descartado; 755: el servicio backup lo
# lee). Al crearse vacío, docker le copia este dueño y estos permisos.
RUN apk add --no-cache libxml2 lz4-libs zstd-libs libbz2 jq \
    && install -d -o postgres -g postgres -m 700 /etc/pgbackrest \
    && install -d -o postgres -g postgres -m 755 /var/lib/timeclock-pitr
COPY --from=pgbackrest-build /out/pgbackrest /usr/local/bin/pgbackrest
COPY --chmod=755 postgres/start.sh /usr/local/bin/timeclock-postgres.sh
COPY --chmod=755 postgres/pgbackrest.sh /usr/local/bin/timeclock-pgbackrest
# Los parámetros los calcula start.sh (README "Parámetros de PostgreSQL"); sin CMD: start.sh arma toda la orden.
ENTRYPOINT ["/usr/local/bin/timeclock-postgres.sh"]
CMD []

# ---------- Imagen de desarrollo/calidad (no se usa en producción) ----------
#   docker build --target dev -t time-clock-backend-dev backend
#   docker run --rm -v "$PWD/backend:/app" time-clock-backend-dev   # pruebas + anomalías
FROM runtime AS dev
USER root
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
# Los modelos ONNX fuera de /app: al montar el código del proyecto en /app para las pruebas siguen
# disponibles y el motor facial REAL también se prueba (tests/test_face_engine_real.py).
# Legibles para cualquier usuario: las pruebas corren con el uid de quien las lanza (-u).
RUN cp -r /app/models /opt/models && chmod -R a+rX /opt/models
ENV TEST_FACE_MODELS_DIR=/opt/models
USER appuser
ENTRYPOINT []
CMD ["python", "scripts/quality.py"]
