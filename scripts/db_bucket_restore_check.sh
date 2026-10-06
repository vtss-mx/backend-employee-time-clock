#!/bin/bash
# Simulacro de la COPIA CIFRADA de los respaldos (BACKUP_UPLOAD) de ida y vuelta (README "Respaldos y restauración" →
# "Restaurar desde el bucket"): el desastre del 2026-10-05 fue perder el equipo completo; esto prueba que con solo el
# bucket y el .env la base vuelve, tabla por tabla.
#
# Todo AISLADO y desechable (nombres timeclock-bucket-check-<pid>-*): su red, un PostgreSQL de origen, un bucket FALSO
# (fsouza/fake-gcs-server, nunca el bucket real) y un emisor de tokens falso para la llave de la cuenta de servicio de
# prueba. El código es el REAL de la imagen del servicio backup: `python -m app.cli db backup` y `db fetch-backup`.
#
#   docker build --target backup -t time-clock-backend-backup backend-employee-time-clock
#   scripts/db_bucket_restore_check.sh                    # BUCKET_CHECK_EMPLOYEES (20000) empleados sembrados
#
# Pasos (el resumen sale al final):
#   1. Origen con el esquema REAL (migraciones + roles) y datos (una empresa y sus empleados: perf/seed.py).
#   2. Respaldo con la copia al bucket encendida y partes de 1 MB: pg_dump, verificación, cifrado por partes y
#      manifiesto cifrado. Ningún objeto del bucket es legible.
#   3. La carpeta local se borra (el desastre). `fetch-backup --list` encuentra la copia en el bucket; `fetch-backup`
#      la baja, la descifra y verifica el SHA-256 de cada parte y del total y su índice.
#   4. scripts/db_restore_check.sh la restaura en OTRO PostgreSQL aislado y compara las filas de cada tabla con las
#      del origen (deben ser idénticas).
set -euo pipefail
IMAGE="${RESTORE_IMAGE:-time-clock-backend-backup}"
GCS_IMAGE="${BUCKET_CHECK_GCS_IMAGE:-fsouza/fake-gcs-server:1.56.1}"
EMPLOYEES="${BUCKET_CHECK_EMPLOYEES:-20000}"
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="timeclock-bucket-check-$$"
NET="$RUN"
WORK="$(mktemp -d)"
REPORT="$WORK/report.txt"
cleanup() {
  docker rm -f "$RUN-db" "$RUN-gcs" "$RUN-token" >/dev/null 2>&1 || true
  docker volume rm "$RUN-backups" "$RUN-key" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
elapsed() { python3 -c "import sys; print(f'{float(sys.argv[2]) - float(sys.argv[1]):.1f}')" "$1" "$2"; }

docker network create "$NET" >/dev/null
docker run -d --name "$RUN-gcs" --network "$NET" "$GCS_IMAGE" -scheme http -port 4443 -backend memory \
  -external-url "http://$RUN-gcs:4443" >/dev/null
# Emisor de tokens de OAuth falso: la llave de prueba firma su JWT como la real y recibe un token (sin red externa).
docker run -d --name "$RUN-token" --network "$NET" --entrypoint python "$IMAGE" -c '
import http.server, json
class Token(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"access_token": "bucket-check", "expires_in": 3600, "token_type": "Bearer"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
http.server.HTTPServer(("0.0.0.0", 8080), Token).serve_forever()' >/dev/null
docker run -d --name "$RUN-db" --network "$NET" --shm-size=256m -e POSTGRES_USER=timeclock \
  -e POSTGRES_PASSWORD=bucket-check -e POSTGRES_DB=timeclock postgres:16-alpine >/dev/null
until docker exec "$RUN-db" pg_isready -q -U timeclock -d timeclock 2>/dev/null; do sleep 1; done
sleep 2

# Configuración: la de una instalación nueva (el generador) más el bucket de prueba. Solo en este simulacro,
# STORAGE_EMULATOR_HOST (lo lee el cliente oficial de Google, no la app) apunta el bucket al falso.
docker run --rm --entrypoint python "$IMAGE" scripts/generate_secrets.py > "$WORK/env"
URL="postgresql+psycopg://timeclock:bucket-check@$RUN-db:5432/timeclock"
cat >> "$WORK/env" <<EOF
POSTGRES_PASSWORD=bucket-check
DATABASE_URL=$URL
DATABASE_DIRECT_URL=$URL
GCS_BUCKET=bucket-check
GCS_PREFIX=check
GCS_CREDENTIALS_FILE=/key/gcs.json
BACKUP_UPLOAD=true
BACKUP_CHUNK_MB=1
STORAGE_EMULATOR_HOST=http://$RUN-gcs:4443
PERF_ENVIRONMENT=1
EOF
app() { docker run --rm --network "$NET" --env-file "$WORK/env" -v "$RUN-backups:/backups" -v "$RUN-key:/key" "$@"; }
app -u root --entrypoint python "$IMAGE" -c "
import json
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
account = {'type': 'service_account', 'project_id': 'bucket-check', 'private_key_id': 'check', 'private_key': pem.decode(),
           'client_email': 'check@bucket-check.iam.gserviceaccount.com', 'client_id': '1',
           'token_uri': 'http://$RUN-token:8080/token'}
open('/key/gcs.json', 'w').write(json.dumps(account))
import os; os.chmod('/key/gcs.json', 0o444)"
docker exec "$RUN-db" wget -q -O /dev/null --header 'Content-Type: application/json' \
  --post-data '{"name":"bucket-check"}' "http://$RUN-gcs:4443/storage/v1/b?project=bucket-check"

echo "== 1. Origen: esquema real y $EMPLOYEES empleados =="
app --entrypoint sh "$IMAGE" -c 'alembic upgrade head && python -m app.cli db roles' > "$WORK/migrate.log" 2>&1 \
  || { cat "$WORK/migrate.log" >&2; exit 1; }
app --entrypoint python "$IMAGE" -m app.cli create-company --email origen@bucket-check.com --password 'Origen12345' >/dev/null
app -i --entrypoint python "$IMAGE" - "$EMPLOYEES" < "$HERE/../perf/seed.py" >/dev/null
docker exec -i "$RUN-db" psql -U timeclock -d timeclock -qAt -v ON_ERROR_STOP=1 < "$HERE/db_table_counts.sql" > "$WORK/expected.txt"
ROWS=$(awk -F'|' '{ total += $2 } END { print total }' "$WORK/expected.txt")
DB_MB=$(docker exec "$RUN-db" psql -U timeclock -d timeclock -qAt -c "SELECT round(pg_database_size('timeclock') / 1048576.0, 2)")
say "Origen: $ROWS filas en $(wc -l < "$WORK/expected.txt" | tr -d ' ') tablas, ${DB_MB} MB"

echo "== 2. Respaldo con la copia cifrada al bucket =="
t0=$(now)
app --entrypoint python "$IMAGE" -m app.cli db backup | tee -a "$REPORT"
say "Respaldo + copia cifrada: $(elapsed "$t0" "$(now)") s"
grep -q "copia al bucket: done" "$REPORT" || { echo "✖ La copia no quedó completa en el bucket" >&2; exit 1; }
LISTING=$(docker exec "$RUN-db" wget -q -O- "http://$RUN-gcs:4443/storage/v1/b/bucket-check/o?prefix=check/backups/")
OBJECTS=$(printf '%s' "$LISTING" | grep -o '"name":"check/backups/[^"]*"' | wc -l | tr -d ' ')
say "Objetos en el bucket: $OBJECTS (partes de 1 MB y el manifiesto, todos cifrados)"
# Ninguno es legible: ninguno empieza con la firma de pg_dump ni contiene el JSON del manifiesto en claro.
for name in $(printf '%s' "$LISTING" | grep -o '"name":"check/backups/[^"]*"' | cut -d'"' -f4); do
  url="http://$RUN-gcs:4443/download/storage/v1/b/bucket-check/o/$(printf '%s' "$name" | sed 's#/#%2F#g')?alt=media"
  head=$(docker exec "$RUN-db" sh -c "wget -q -O- '$url' | head -c 6")
  [ "$head" = gAAAAA ] || { echo "✖ $name no está cifrado (empieza con $head)" >&2; exit 1; }
done
say "Todos los objetos son tokens cifrados (Fernet): ninguno legible"

echo "== 3. El desastre: la carpeta local se borra; se restaura desde el bucket =="
app -u root --entrypoint sh "$IMAGE" -c 'rm -rf /backups/*'
app --entrypoint python "$IMAGE" -m app.cli db fetch-backup --list | tee -a "$REPORT"
mkdir -p "$WORK/restore" && chmod 777 "$WORK/restore"
t0=$(now)
app -v "$WORK/restore:/restore" --entrypoint python "$IMAGE" -m app.cli db fetch-backup --dir /restore | tee -a "$REPORT"
say "Bajar, descifrar y verificar: $(elapsed "$t0" "$(now)") s"
DUMP=$(ls "$WORK"/restore/*.dump)

echo "== 4. Restauración aislada y filas tabla por tabla =="
t0=$(now)
RESTORE_IMAGE="$IMAGE" "$HERE/db_restore_check.sh" "$DUMP" "$WORK/expected.txt" | tee -a "$REPORT"
say "Restaurar y comparar: $(elapsed "$t0" "$(now)") s"

echo
echo "================ Resumen ================"
cat "$REPORT"
echo "✔ Copia cifrada de ida y vuelta: bucket → descifrada → restaurada con las mismas filas en cada tabla"
