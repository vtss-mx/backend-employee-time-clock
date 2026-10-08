"""Comandos de administración.

python -m app.cli create-admin --email admin@plataforma.com
python -m app.cli create-company --email admin@empresa.com
python -m app.cli create-company --email admin@empresa.com --password 'Admin1234'
python -m app.cli purge          (depura lo vencido ahora; útil desde un cron externo)
python -m app.cli storage status (bucket de imágenes: configurado, guardadas, cola de borrado, errores)
python -m app.cli storage lifecycle [--apply]
                                 (regla del bucket que borra las copias cifradas vencidas de <GCS_PREFIX>/backups/;
                                  sin --apply solo muestra lo actual y lo que quedaría)
python -m app.cli db roles       (crea o pone al día los roles de la base y sus permisos; lo corre `migrate`)
python -m app.cli db backup      (un respaldo ahora; --loop: el servicio `backup`, cada BACKUP_INTERVAL_HOURS, que
                                  además vigila PITR y avisa al ADMIN)
python -m app.cli db fetch-backup [--list] [--name NOMBRE] [--dir CARPETA]
                                 (baja del bucket una copia cifrada, la descifra y la verifica; sin --name, la más
                                  reciente; luego scripts/db_restore_check.sh)
python -m app.cli db pitr-status (archivo continuo del WAL y respaldos base: estado y avisos; 1 si hay avisos)
python -m app.cli ipdb refresh   (base local de IP DB-IP Lite: descarga, verifica y reemplaza; --force: ya mismo)
python -m app.cli cache clear-catalogs
                                 (borra las instantáneas de catálogos de la caché compartida (Redis) para que todas las
                                  réplicas recarguen de la base; lo corre `migrate` al terminar. Sin Redis no hace nada)
"""

import argparse
import getpass
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from sqlalchemy import Engine

from app.core.cache import shared_cache
from app.core.config import settings
from app.core.database import build_engine, platform_session
from app.core.db_roles import RolePlan, provision
from app.core.object_storage import StorageError
from app.services import db_backup, ip_database, pitr_monitor, storage_jobs, storage_lifecycle
from app.services.bootstrap import UserFactory, create_admin_user, create_company_user
from app.services.catalog_service import clear_shared_catalogs
from app.services.error_reporter import ErrorReportFlusher, install_log_handler
from app.services.maintenance_service import run_once


def _create_admin(args: argparse.Namespace) -> int:
    return _create(args, "ADMIN", create_admin_user)


def _create_company(args: argparse.Namespace) -> int:
    return _create(args, "COMPANY", create_company_user)


def _create(args: argparse.Namespace, label: str, create: UserFactory) -> int:
    password = args.password or getpass.getpass("Contraseña: ")
    if not args.password and password != getpass.getpass("Confirmar contraseña: "):
        print("Las contraseñas no coinciden", file=sys.stderr)
        return 1
    with platform_session() as db:
        try:
            user = create(db, args.email.lower(), password)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    print(f"Usuario {label} creado: id={user.id} email={user.email}")
    return 0


def _purge(_: argparse.Namespace) -> int:
    removed = run_once()
    if removed is None:
        print("Otra instancia está depurando en este momento; no se hizo nada.")
        return 0
    print("Depurado: " + ", ".join(f"{name}={count}" for name, count in removed.items()))
    return 0


def _ipdb_refresh(args: argparse.Namespace) -> int:
    """La base local de IP ahora (la misma tarea del mantenimiento): 0 si quedó al día, 1 si algún archivo falló."""
    results = ip_database.refresh_if_due(force=args.force)
    if not results:
        print("Nada que hacer: la actualización está apagada (IP_DB_REFRESH_ENABLED) o los archivos están al día.")
        return 0
    print("Base local de IP: " + ", ".join(f"{kind}={result}" for kind, result in results.items()))
    return 1 if ip_database.FAILED in results.values() else 0


def _storage_status(_: argparse.Namespace) -> int:
    with platform_session() as db:
        state = storage_jobs.status(db)
    where = f"gs://{state.bucket}/{state.prefix}/" if state.configured else f"apagado ({state.reason})"
    print(f"Almacenamiento de imágenes: {where}")
    for image in state.images:
        print(f"- {image.label}: en el bucket: {image.stored}")
    for task in state.tasks:
        print(f"- {task.label}: {task.pending}")
        if task.last_error:
            print(f"  último error ({task.last_error_at:%Y-%m-%d %H:%M}): {task.last_error}")
    return 0


def _db_roles(_: argparse.Namespace, direct: Engine | None = None) -> int:
    """Roles de mínimo privilegio (app/core/db_roles.py) con la conexión directa del dueño, en una transacción."""
    try:
        plan = RolePlan.from_settings()
    except ValueError as exc:
        print(f"Sin roles que aprovisionar: {exc}")
        return 0
    engine = direct or build_engine(settings.DATABASE_DIRECT_URL)
    try:
        with engine.begin() as conn:
            provision(conn, plan)
    finally:
        engine.dispose()
    readonly = f", solo lectura {plan.readonly}" if plan.readonly else ""
    print(f"Roles al día: API {plan.app}, plataforma {plan.platform}{readonly}")
    return 0


def _storage_lifecycle(args: argparse.Namespace) -> int:
    """La regla de ciclo de vida de las copias cifradas (`storage_lifecycle`): sin --apply solo muestra."""
    try:
        plan = storage_lifecycle.lifecycle(apply=args.apply)
    except StorageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    condition = storage_lifecycle.managed_rule()["condition"]
    print(f"Bucket gs://{plan.bucket}: borrar {condition['matchesPrefix'][0]} con más de {condition['age']} días")
    print("(BACKUP_RETENTION_DAYS + BACKUP_LIFECYCLE_MARGIN_DAYS; nunca <GCS_PREFIX>/pitr/: pgBackRest la administra)")
    if not plan.changed:
        print("Sin cambios: el bucket ya tiene la regla.")
    elif args.apply:
        print("Aplicada.")
    else:
        print("Sin aplicar (vista previa). Para aplicarla: python -m app.cli storage lifecycle --apply")
    print("Ciclo de vida que queda (gcloud storage buckets update gs://BUCKET --lifecycle-file=ARCHIVO):")
    print(plan.gcloud_file())
    return 0


def _cache_clear_catalogs(_: argparse.Namespace) -> int:
    """Las instantáneas de catálogos de Redis, fuera: las réplicas recargan de la base al vencer su copia local.
    Nunca falla el despliegue (regla 7): sin Redis, o con Redis caído, lo dice y termina bien (vencen solas)."""
    if not shared_cache().enabled:
        print("Caché compartida apagada (REDIS_HOST y REDIS_URL vacíos): nada que limpiar")
        return 0
    removed = clear_shared_catalogs()
    if removed is None:
        print(f"Redis no respondió: las instantáneas de catálogos vencen solas en {settings.CATALOG_REDIS_SECONDS:g} s")
        return 0
    print(f"Instantáneas de catálogos borradas de la caché compartida: {removed}")
    return 0


def _stop(_signum: int, _frame: FrameType | None) -> None:
    """docker stop (SIGTERM): sale del ciclo para guardar los errores pendientes antes de terminar."""
    raise SystemExit(0)


def _backup_service() -> int:
    """El servicio `backup`: respaldos, sus copias al bucket y, con PITR, el monitor. Sus fallas llegan al ADMIN
    ("Errores del sistema"): el log del proceso se conecta al registro de errores y se guarda en lotes."""
    logging.basicConfig(level=settings.LOG_LEVEL.upper(), format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    install_log_handler()
    flusher = ErrorReportFlusher(settings.ERROR_REPORT_FLUSH_SECONDS) if settings.ERROR_REPORT_FLUSH_SECONDS else None
    if flusher:
        flusher.start()
    signal.signal(signal.SIGTERM, _stop)
    monitor = pitr_monitor.PitrMonitor() if settings.PITR_ENABLED else None
    try:
        db_backup.run_forever(settings.BACKUP_INTERVAL_HOURS, monitor=monitor.check if monitor else None)
    finally:
        if flusher:
            flusher.stop()
    return 0


def _db_backup(args: argparse.Namespace) -> int:
    if args.loop:
        if not settings.BACKUP_INTERVAL_HOURS and not settings.PITR_ENABLED:
            print("BACKUP_INTERVAL_HOURS=0 y PITR apagado: el servicio no tiene nada que hacer")
            return 0
        return _backup_service()
    try:
        backup = db_backup.backup_now()
    except db_backup.BackupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Respaldo: {backup.path} ({backup.size} bytes, sha256 {backup.sha256}); copia al bucket: {backup.upload}")
    print(f"Vencidos borrados: {db_backup.prune()}")
    return 0


def _db_fetch_backup(args: argparse.Namespace) -> int:
    """Restaurar desde el bucket, paso 1: bajar, descifrar y verificar (el paso 2 es scripts/db_restore_check.sh)."""
    try:
        if args.list:
            names = db_backup.remote_backups()
            print("\n".join(names) if names else "No hay copias completas en el bucket")
            return 0
        path = db_backup.fetch(args.name, Path(args.dir))
    except (StorageError, db_backup.BackupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Copia verificada (SHA-256 de cada parte y del total, pg_restore --list): {path}")
    return 0


def _db_pitr_status(_: argparse.Namespace) -> int:
    """El estado de PITR y los avisos que daría al ADMIN (sin registrarlos): 0 si todo está bien, 1 si hay avisos."""
    if not settings.PITR_ENABLED:
        print("PITR apagado (PITR_ENABLED=false)")
        return 0
    monitor = pitr_monitor.PitrMonitor()
    snapshot = monitor.snapshot()
    for line in pitr_monitor.describe(snapshot):
        print(line)
    alerts = pitr_monitor.evaluate(snapshot, datetime.now(UTC), watching_since=datetime.min.replace(tzinfo=UTC))
    for alert in alerts:
        print(f"AVISO {alert.key}: {alert.message}")
    print("Sin avisos" if not alerts else f"{len(alerts)} aviso(s)")
    return 1 if alerts else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="Crear un administrador de la plataforma (ADMIN)")
    admin.add_argument("--email", required=True)
    admin.add_argument("--password", help="Si se omite se solicita de forma interactiva")
    admin.set_defaults(func=_create_admin)
    create = sub.add_parser("create-company", help="Crear un administrador de la primera empresa (COMPANY)")
    create.add_argument("--email", required=True)
    create.add_argument("--password", help="Si se omite se solicita de forma interactiva")
    create.set_defaults(func=_create_company)
    purge = sub.add_parser("purge", help="Depurar lo vencido (sesiones, retos, huellas, QR, contadores)")
    purge.set_defaults(func=_purge)
    storage = sub.add_parser("storage", help="Imágenes en el bucket (Google Cloud Storage)")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)
    status = storage_sub.add_parser("status", help="Configurado, imágenes guardadas, cola de borrado y errores")
    status.set_defaults(func=_storage_status)
    lifecycle = storage_sub.add_parser("lifecycle", help="Regla del bucket que borra las copias de respaldo vencidas")
    lifecycle.add_argument("--apply", action="store_true", help="Escribirla en el bucket (sin esto solo se muestra)")
    lifecycle.set_defaults(func=_storage_lifecycle)
    database = sub.add_parser("db", help="Base de datos: roles, respaldos y recuperación a un punto en el tiempo")
    database_sub = database.add_subparsers(dest="db_command", required=True)
    roles = database_sub.add_parser("roles", help="Crear o poner al día los roles y permisos (idempotente)")
    roles.set_defaults(func=_db_roles)
    backup = database_sub.add_parser("backup", help="Respaldo pg_dump (y su copia cifrada al bucket)")
    backup.add_argument("--loop", action="store_true", help="Servicio: un respaldo cada BACKUP_INTERVAL_HOURS")
    backup.set_defaults(func=_db_backup)
    fetch = database_sub.add_parser(
        "fetch-backup", help="Bajar del bucket una copia cifrada, descifrarla y verificarla"
    )
    fetch.add_argument("--list", action="store_true", help="Solo listar las copias completas del bucket")
    fetch.add_argument("--name", help="Copia (p. ej. timeclock-20261006T030000Z); sin esto, la más reciente")
    fetch.add_argument("--dir", default=f"{settings.BACKUP_DIR}/restore", help="Carpeta destino")
    fetch.set_defaults(func=_db_fetch_backup)
    pitr = database_sub.add_parser("pitr-status", help="Archivo continuo del WAL y respaldos base: estado y avisos")
    pitr.set_defaults(func=_db_pitr_status)
    ipdb = sub.add_parser("ipdb", help="Base local de IP (DB-IP Lite: país y sistema autónomo)")
    ipdb_sub = ipdb.add_subparsers(dest="ipdb_command", required=True)
    refresh = ipdb_sub.add_parser("refresh", help="Descargar, verificar y reemplazar los archivos si toca")
    refresh.add_argument("--force", action="store_true", help="Aunque los archivos sean recientes")
    refresh.set_defaults(func=_ipdb_refresh)
    cache = sub.add_parser("cache", help="Caché compartida entre réplicas (Redis)")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    clear_catalogs = cache_sub.add_parser("clear-catalogs", help="Borrar las instantáneas de catálogos compartidas")
    clear_catalogs.set_defaults(func=_cache_clear_catalogs)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
