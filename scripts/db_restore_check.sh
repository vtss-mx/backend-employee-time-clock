#!/bin/bash
# Prueba de RESTAURACIÓN de un respaldo (README "Respaldos y restauración"): un respaldo que nunca se restauró no es
# un respaldo. Restaura el archivo en un PostgreSQL 16 AISLADO (sin puertos ni volúmenes: jamás la base de trabajo),
# revisa que la base quedó completa y lo borra todo al terminar.
#
#   scripts/db_restore_check.sh /ruta/timeclock-20261005T030000Z.dump
#   docker compose run --rm -v "$PWD/restore:/backups" backup ...   # (un respaldo del volumen `backups`: cópialo antes)
#   Desde el bucket (la carpeta local se perdió): python -m app.cli db fetch-backup (baja, descifra y verifica) y luego
#   este script con la copia; el simulacro completo es scripts/db_bucket_restore_check.sh.
#
# Revisa: versión de las migraciones, tablas, seguridad por fila (tablas con RLS forzada y sus políticas), tablas
# particionadas y su función de mantenimiento, y las filas de cada tabla (las imprime; con un segundo argumento,
# un archivo con las filas esperadas `tabla|filas`, las compara y falla si alguna difiere).
# Requiere Docker y la imagen del servicio de respaldos (su pg_restore lee lo que escribió su pg_dump):
#   docker build --target backup -t time-clock-backend-backup backend-employee-time-clock
set -eu
DUMP="${1:?Uso: scripts/db_restore_check.sh <respaldo.dump> [filas-esperadas.txt]}"
EXPECTED="${2:-}"
IMAGE="${RESTORE_IMAGE:-time-clock-backend-backup}"
NAME=timeclock-restore-check
NET=timeclock-restore-check
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup
docker network create "$NET" >/dev/null
docker run -d --rm --name "$NAME" --network "$NET" --shm-size=512m \
  -e POSTGRES_USER=restore -e POSTGRES_PASSWORD=restore -e POSTGRES_DB=restored postgres:16-alpine >/dev/null
until docker exec "$NAME" pg_isready -U restore -d restored >/dev/null 2>&1; do sleep 1; done

STARTED=$(date +%s)
# Sin dueños ni permisos del servidor original (sus roles no existen aquí): lo restaurado queda del usuario de la
# prueba. La estructura (políticas, RLS forzada, particiones, función) y los datos son los del respaldo.
docker run --rm --network "$NET" -v "$(cd "$(dirname "$DUMP")" && pwd):/dump:ro" --entrypoint pg_restore "$IMAGE" \
  --no-owner --no-privileges --exit-on-error --jobs=4 \
  --dbname="postgresql://restore:restore@$NAME:5432/restored" "/dump/$(basename "$DUMP")"
echo "Restaurado en $(( $(date +%s) - STARTED )) s"

psql() { docker exec -i "$NAME" psql -U restore -d restored -qAt -v ON_ERROR_STOP=1 "$@"; }
psql -c "SELECT 'migraciones: ' || version_num FROM public.alembic_version"
psql <<'SQL'
SELECT 'tablas (sin particiones): ' || count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('auth','tenancy','workforce','biometrics','attendance','catalog','ops','billing')
  AND c.relkind IN ('r', 'p') AND NOT c.relispartition;
SELECT 'seguridad por fila forzada: ' || count(*) FILTER (WHERE relrowsecurity AND relforcerowsecurity)
       || ' tablas, ' || (SELECT count(*) FROM pg_policies WHERE policyname = 'tenant_isolation') || ' políticas'
FROM pg_class;
SELECT 'particionadas: ' || count(*) || ' tablas, '
       || (SELECT count(*) FROM pg_class WHERE relispartition AND relkind IN ('r', 'p')) || ' particiones'
FROM pg_partitioned_table;
SELECT 'función de particiones: ' || (to_regprocedure('ops.ensure_partitions(text, date, integer, date)') IS NOT NULL);
SQL
COUNTS=$(psql < "$(dirname "$0")/db_table_counts.sql")
echo "$COUNTS" | awk -F'|' '{ total += $2 } END { printf "filas restauradas: %d en %d tablas\n", total, NR }'
if [ -n "$EXPECTED" ]; then
  if diff <(sort "$EXPECTED") <(echo "$COUNTS" | sort) >/dev/null 2>&1; then
    echo "✔ Las filas de cada tabla son las esperadas"
  else
    echo "✖ Las filas restauradas no son las esperadas:"; diff <(sort "$EXPECTED") <(echo "$COUNTS" | sort) || true
    exit 1
  fi
else
  echo "$COUNTS"
fi
echo "✔ Respaldo restaurable: $(basename "$DUMP")"
