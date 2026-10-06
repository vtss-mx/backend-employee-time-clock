"""Respaldos de la base de datos (README "Respaldos y restauración"; regla 15 del `AGENTS.md` raíz).

- `pg_dump` en formato *custom* (comprimido, restaurable por partes y en paralelo con `pg_restore -j`), directo a
  PostgreSQL con el DUEÑO (no por PgBouncer: un respaldo es una transacción larga de solo lectura con una foto
  consistente de toda la base). Se escribe con un nombre temporal y se renombra al terminar: nunca queda un
  respaldo a medias con nombre válido. Antes de darlo por bueno se lee su índice (`pg_restore --list`).
- Retención: los de más de `BACKUP_RETENTION_DAYS` días se borran (de la carpeta y del bucket). Si la carpeta se
  pierde, la regla de ciclo de vida del bucket (`storage_lifecycle`) borra las copias vencidas.
- Copia fuera del servidor (`BACKUP_UPLOAD`, decisión del dueño: encendida): al bucket propio de la plataforma,
  **cifrada antes de salir** con `DATA_ENCRYPTION_KEY` (la misma capa de cifrado y de almacenamiento que las imágenes:
  `app.core.crypto`, `app.core.object_storage`), por partes de `BACKUP_CHUNK_MB` (nunca el respaldo completo en
  memoria) y con un manifiesto cifrado (partes, SHA-256 de cada parte y del respaldo) para verificarla al bajarla.
  Jamás una copia legible. **El manifiesto local se escribe ANTES de subir**: si el bucket no responde, el respaldo
  local queda completo y anotado como pendiente, la falla llega al ADMIN y la subida se reintenta cada
  `BACKUP_RETRY_MINUTES` (idempotente: cada objeto lleva en sus metadatos el SHA-256 de su contenido legible, así que
  un reintento reconoce lo que ya subió y no lo repite ni lo pisa).
- Restaurar desde el bucket (`fetch`): baja las partes, las descifra (Fernet autentica cada una), verifica el SHA-256
  de cada parte y del respaldo completo y su índice (`pg_restore --list`); luego `scripts/db_restore_check.sh` lo
  restaura en un PostgreSQL aislado. Funciona sin la carpeta local (el desastre del 2026-10-05: se perdió el equipo).
- El servicio `backup` de docker compose corre `python -m app.cli db backup --loop` (`run_forever`); con PITR también
  revisa el archivo continuo del WAL (`app/services/pitr_monitor.py`). `python -m app.cli db backup` hace uno a mano.
"""

import hashlib
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any, Final

from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.crypto import decrypt_bytes, encrypt_bytes
from app.core.object_storage import ObjectExists, ObjectStorage, StorageError, get_storage

logger = logging.getLogger(__name__)

#: Extensión de un respaldo terminado (`<base>-<UTC>.dump`) y de su manifiesto.
DUMP_SUFFIX: Final = ".dump"
MANIFEST_SUFFIX: Final = ".manifest.json"
#: El manifiesto cifrado de cada copia en el bucket: la marca de que la copia está completa.
MANIFEST_OBJECT: Final = "manifest.json.enc"
#: Estado de la copia al bucket: sin copia (BACKUP_UPLOAD apagado), pendiente (falló: se reintenta) o arriba.
UPLOAD_OFF, UPLOAD_PENDING, UPLOAD_DONE = "off", "pending", "done"
#: Tope de objetos al listar las copias del bucket (catorce días de respaldos diarios de decenas de partes caben con
#: mucho margen): nunca una lista sin límite.
_MAX_LISTED: Final = 100_000
_STAMP: Final = "%Y%m%dT%H%M%SZ"


class BackupError(Exception):
    """El respaldo no se pudo hacer o no se pudo verificar (el mensaje dice qué paso falló)."""


@dataclass
class Backup:
    """Un respaldo terminado y verificado (y lo que se sabe de su copia en el bucket)."""

    path: Path
    size: int
    sha256: str
    created_at: datetime
    #: Objetos cifrados en el bucket (los que ya subieron, aunque la copia esté pendiente: la depuración los borra).
    objects: list[str] = field(default_factory=list)
    upload: str = UPLOAD_OFF


def _libpq_env() -> tuple[list[str], dict[str, str]]:
    """Destino de pg_dump (sin la contraseña en la línea de comandos: va en PGPASSWORD) a partir de la conexión
    directa del dueño (`DATABASE_DIRECT_URL`)."""
    url = make_url(settings.DATABASE_DIRECT_URL)
    args = ["--host", url.host or "localhost", "--port", str(url.port or 5432), "--username", url.username or ""]
    env = {
        **os.environ,
        "PGPASSWORD": url.password or "",
        "PGCONNECT_TIMEOUT": str(settings.DB_CONNECT_TIMEOUT_SECONDS),
    }
    return [*args, "--dbname", url.database or settings.POSTGRES_DB], env


def _run(command: list[str], env: dict[str, str], step: str) -> None:
    try:
        result = subprocess.run(  # noqa: S603  (argumentos fijos, sin shell)
            command, env=env, capture_output=True, text=True, timeout=settings.BACKUP_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise BackupError(f"{step}: no está instalado {command[0]} (imagen del servicio backup)") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"{step}: pasó de {settings.BACKUP_TIMEOUT_SECONDS} s") from exc
    if result.returncode != 0:
        raise BackupError(f"{step}: {result.stderr.strip()[-500:]}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunks(path: Path) -> Iterator[bytes]:
    size = settings.BACKUP_CHUNK_MB * 1024 * 1024
    with path.open("rb") as handle:
        while chunk := handle.read(size):
            yield chunk


def _remote_base(stem: str) -> str:
    return f"{settings.storage_prefix}/backups/{stem}"


def _manifest_path(dump: Path) -> Path:
    return dump.with_name(dump.stem + MANIFEST_SUFFIX)


def _save(backup: Backup) -> None:
    """El manifiesto local, escrito de golpe (temporal + rename): nunca uno a medias."""
    data = {**asdict(backup), "path": str(backup.path), "created_at": backup.created_at.isoformat()}
    target = _manifest_path(backup.path)
    partial = target.with_suffix(".partial")
    partial.write_text(json.dumps(data, indent=2), encoding="utf-8")
    partial.replace(target)


def _load(manifest: Path) -> Backup:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    objects = list(data.get("objects", []))
    # Manifiestos anteriores a la copia pendiente: sin "upload"; con objetos, la copia se completó.
    upload = data.get("upload") or (UPLOAD_DONE if objects else UPLOAD_OFF)
    created = datetime.fromisoformat(data["created_at"])
    return Backup(Path(data["path"]), int(data["size"]), str(data["sha256"]), created, objects, upload)


def local_backups() -> list[Backup]:
    """Los respaldos de la carpeta, del más viejo al más nuevo (por su manifiesto). Un manifiesto ilegible se registra
    y se omite: nunca frena la depuración ni el reintento de los demás."""
    found: list[Backup] = []
    for path in sorted(Path(settings.BACKUP_DIR).glob(f"*{MANIFEST_SUFFIX}")):
        try:
            found.append(_load(path))
        except OSError, ValueError, KeyError, TypeError:
            logger.exception("Manifiesto de respaldo ilegible (se omite): %s", path.name)
    return found


def _put_once(storage: ObjectStorage, name: str, data: bytes, metadata: dict[str, str]) -> None:
    """Sube "solo si no existe". Si ya estaba (un reintento tras una falla a medias) se reconoce por el SHA-256 de su
    contenido legible en sus metadatos (el cifrado cambia en cada intento, el contenido no); otro objeto con ese nombre
    es un error, nunca se pisa."""
    try:
        storage.put(name, data, metadata)
    except ObjectExists:
        info = storage.stat(name)
        if info is None or info.metadata.get("sha256") != metadata["sha256"]:
            raise BackupError(f"en el bucket ya hay otro objeto llamado {name}") from None


def _upload(backup: Backup) -> None:
    """Sube el respaldo CIFRADO por partes y al final su manifiesto (la marca de copia completa). Va anotando cada
    objeto en `backup.objects` para que, si falla a medias, la depuración igual los borre al vencer."""
    storage = get_storage()
    base = _remote_base(backup.path.stem)
    names: list[str] = []
    digests: list[str] = []
    for number, chunk in enumerate(_chunks(backup.path), start=1):
        name, digest = f"{base}/part-{number:05d}.enc", hashlib.sha256(chunk).hexdigest()
        _put_once(storage, name, encrypt_bytes(chunk), {"part": str(number), "sha256": digest})
        names.append(name)
        digests.append(digest)
        if name not in backup.objects:
            backup.objects.append(name)
    manifest = {
        "file": backup.path.name,
        "size": backup.size,
        "sha256": backup.sha256,
        "created_at": backup.created_at.isoformat(),
        "parts": names,
        "part_sha256": digests,
    }
    name = f"{base}/{MANIFEST_OBJECT}"
    _put_once(
        storage, name, encrypt_bytes(json.dumps(manifest).encode()), {"parts": str(len(names)), "sha256": backup.sha256}
    )
    backup.objects.append(name)  # el último paso: nunca estaba (una falla antes de aquí no lo anota)
    backup.upload = UPLOAD_DONE


def _try_upload(backup: Backup) -> bool:
    """Una subida que puede fallar sin perder el respaldo: la falla se registra (llega al ADMIN) y queda pendiente."""
    try:
        _upload(backup)
    except StorageError, BackupError, OSError:
        logger.exception(
            "No se pudo subir la copia cifrada de %s al bucket: el respaldo local se conserva y se reintenta (%s min)",
            backup.path.name,
            settings.BACKUP_RETRY_MINUTES,
        )
        return False
    finally:
        _save(backup)
    return True


def backup_now(now: datetime | None = None) -> Backup:
    """Un respaldo completo: pg_dump, verificación, manifiesto local y (si se pidió) la copia cifrada al bucket. Una
    falla del bucket no hace fallar el respaldo (queda pendiente); una de pg_dump sí (`BackupError`)."""
    moment = now or datetime.now(UTC)
    folder = Path(settings.BACKUP_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{settings.POSTGRES_DB}-{moment.strftime(_STAMP)}{DUMP_SUFFIX}"
    partial = final.with_suffix(".partial")
    target, env = _libpq_env()
    started = time.monotonic()
    try:
        _run(["pg_dump", "--format=custom", "--compress=6", "--file", str(partial), *target], env, "pg_dump")
        _run(["pg_restore", "--list", str(partial)], env, "verificación (pg_restore --list)")
        partial.rename(final)
    finally:
        partial.unlink(missing_ok=True)
    backup = Backup(final, final.stat().st_size, _sha256(final), moment)
    backup.upload = UPLOAD_PENDING if settings.BACKUP_UPLOAD else UPLOAD_OFF
    _save(backup)
    if backup.upload == UPLOAD_PENDING:
        _try_upload(backup)
    logger.info(
        "Respaldo %s: %.2f MB en %.1f s; copia al bucket: %s (%s objetos cifrados)",
        final.name,
        backup.size / 1_048_576,
        time.monotonic() - started,
        backup.upload,
        len(backup.objects),
    )
    return backup


def retry_uploads() -> int:
    """Vuelve a subir las copias que quedaron pendientes (bucket caído); devuelve cuántas quedaron completas."""
    pending = [b for b in local_backups() if b.upload == UPLOAD_PENDING and b.path.exists()]
    return sum(_try_upload(backup) for backup in pending)


def prune(now: datetime | None = None) -> int:
    """Borra los respaldos vencidos de la carpeta y sus objetos del bucket (los dice su manifiesto)."""
    limit = (now or datetime.now(UTC)) - timedelta(days=settings.BACKUP_RETENTION_DAYS)
    removed = 0
    for backup in local_backups():
        if backup.created_at >= limit:
            continue
        for name in backup.objects:
            get_storage().delete(name)
        backup.path.unlink(missing_ok=True)
        _manifest_path(backup.path).unlink()
        removed += 1
    return removed


def remote_backups() -> list[str]:
    """Las copias COMPLETAS en el bucket (las que tienen manifiesto), de la más vieja a la más nueva."""
    prefix = f"{settings.storage_prefix}/backups/"
    suffix = f"/{MANIFEST_OBJECT}"
    names = get_storage().list_names(prefix, limit=_MAX_LISTED)
    return sorted(name.removeprefix(prefix).removesuffix(suffix) for name in names if name.endswith(suffix))


def _decrypted(storage: ObjectStorage, name: str) -> bytes:
    try:
        return decrypt_bytes(storage.get(name))
    except ValueError as exc:  # otra llave o un objeto alterado: Fernet autentica cada parte
        raise BackupError(
            f"{name}: no se pudo descifrar (¿DATA_ENCRYPTION_KEY o DATA_ENCRYPTION_PREVIOUS_KEYS?)"
        ) from exc


def _download(storage: ObjectStorage, manifest: dict[str, Any], partial: Path) -> None:
    """Baja, descifra y verifica parte por parte (en orden, una a la vez: nunca el respaldo completo en memoria)."""
    expected = manifest.get("part_sha256") or [None] * len(manifest["parts"])
    digest, size = hashlib.sha256(), 0
    with partial.open("wb") as handle:
        for name, part_sha in zip(manifest["parts"], expected, strict=True):
            chunk = _decrypted(storage, name)
            if part_sha and hashlib.sha256(chunk).hexdigest() != part_sha:
                raise BackupError(f"{name}: su SHA-256 no es el del manifiesto")
            handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    if size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
        raise BackupError(f"el respaldo armado no coincide con su manifiesto ({size} de {manifest['size']} bytes)")


def fetch(name: str | None, folder: Path) -> Path:
    """Baja del bucket la copia `name` (sin nombre, la más reciente), la descifra, la verifica (SHA-256 de cada parte y
    del total, e índice con `pg_restore --list`) y la deja en `folder` lista para `scripts/db_restore_check.sh` o
    para `pg_restore`. Nunca deja un archivo a medias con nombre válido."""
    storage = get_storage()
    available = remote_backups()
    stem = name or (available[-1] if available else None)
    if stem is None:
        raise BackupError(f"no hay copias completas en el bucket bajo {settings.storage_prefix}/backups/")
    if stem not in available:
        raise BackupError(f"no existe la copia {stem} en el bucket (o está incompleta: sin su manifiesto)")
    manifest = json.loads(_decrypted(storage, f"{_remote_base(stem)}/{MANIFEST_OBJECT}"))
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / Path(manifest["file"]).name
    partial = target.with_suffix(".partial")
    try:
        _download(storage, manifest, partial)
        _run(["pg_restore", "--list", str(partial)], dict(os.environ), "verificación (pg_restore --list)")
        partial.rename(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def _due(interval_hours: float, now: datetime) -> bool:
    """¿Toca respaldar? `BACKUP_INTERVAL_HOURS` desde el último respaldo de la carpeta (reiniciar el servicio, p. ej.
    en un despliegue, no hace uno de más); 0 = nunca solo."""
    if interval_hours <= 0:
        return False
    newest = max((backup.created_at for backup in local_backups()), default=None)
    return newest is None or now - newest >= timedelta(hours=interval_hours)


def _tick(interval_hours: float, monitored: bool) -> float:
    """Cada cuánto da una vuelta el servicio: lo menor entre el respaldo, el reintento de la copia y el monitor."""
    candidates = [interval_hours * 3600] if interval_hours > 0 else []
    if settings.BACKUP_UPLOAD:
        candidates.append(settings.BACKUP_RETRY_MINUTES * 60)
    if monitored:
        candidates.append(settings.PITR_CHECK_SECONDS)
    return min(candidates, default=settings.BACKUP_RETRY_MINUTES * 60)


def _dump_if_due(interval_hours: float) -> None:
    if _due(interval_hours, datetime.now(UTC)):
        backup_now()
        prune()


def _attempt(action: Callable[[], object], now: float, what: str) -> float:
    """Una tarea del servicio: si falla se registra (llega al ADMIN) y devuelve cuándo reintentarla."""
    try:
        action()
    except Exception:
        logger.exception("%s (se reintenta en %s min)", what, settings.BACKUP_RETRY_MINUTES)
        return now + settings.BACKUP_RETRY_MINUTES * 60
    return 0.0


def run_forever(
    interval_hours: float,
    *,
    rounds: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monitor: Callable[[], object] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """El servicio `backup`. En cada vuelta: el respaldo si toca (y la depuración; si falla, se reintenta en
    `BACKUP_RETRY_MINUTES`), el reintento de las copias pendientes (a lo más cada `BACKUP_RETRY_MINUTES`: sin
    martillar al bucket caído) y, con PITR, la revisión del monitor. `rounds`: cuántas vueltas (None = siempre). Una
    falla se registra y no detiene nada: el servicio nunca muere por una falla de la base o del bucket."""
    dump_at = upload_at = 0.0
    for _ in count() if rounds is None else range(rounds):
        now = clock()
        # Primero lo pendiente: una copia que falla en esta vuelta espera su turno (no se reintenta en seguida).
        if settings.BACKUP_UPLOAD and now >= upload_at:
            upload_at = now + settings.BACKUP_RETRY_MINUTES * 60
            _attempt(retry_uploads, now, "Falló el reintento de las copias cifradas al bucket")
        if now >= dump_at:
            dump_at = _attempt(lambda: _dump_if_due(interval_hours), now, "Falló el respaldo de la base de datos")
        if monitor is not None:
            monitor()
        sleep(_tick(interval_hours, monitor is not None))
