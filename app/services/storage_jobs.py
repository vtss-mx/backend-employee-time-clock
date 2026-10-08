"""Trabajo del almacenamiento de imágenes FUERA de las peticiones (`image_storage`).

- **Mantenimiento** (`run`, en cada vuelta de `maintenance_service`, una instancia a la vez): vacía la
  cola de borrado contra el bucket (registros rechazados, empleados borrados y objetos de transacciones que
  no se confirmaron). Borrar lo que ya no estaba cuenta como hecho. Topes por vuelta (`GCS_BATCH_SIZE`,
  `GCS_ROUND_SECONDS`); si el bucket no responde, la vuelta para y la siguiente sigue; falla sola y anota
  su resultado en `ops.storage_status` (estado del ADMIN).
- **Estado** (`status`): para el ADMIN y `python -m app.cli storage status`, sin llamadas al bucket.

Las imágenes solo viven en el bucket: no hay nada que migrar desde la BD (migración `0053`).
"""

import logging
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.object_storage import ObjectStorage, get_storage
from app.core.observability import observed
from app.i18n import t
from app.models import StorageStatus
from app.repositories.aggregates import LOG_COUNT_CAP
from app.repositories.storage_repository import StorageRepository
from app.schemas.storage import ObjectStorageStatus, StorageTaskStatus, StoredImageCount
from app.services.image_storage import STORED_IMAGES

logger = logging.getLogger(__name__)

#: Tarea que vacía la cola de borrados (su fila en `ops.storage_status`).
DELETE_TASK = "delete"
DELETED = "objetos borrados del bucket"


def _has_time(deadline: float) -> bool:
    return time.monotonic() < deadline


# ---------------------------------------------------------------- mantenimiento: cola de borrado


@observed("storage.cleanup")
def run(db: Session, now: datetime) -> dict[str, int]:
    """Una vuelta del mantenimiento: borra del bucket lo que la plataforma ya no conserva."""
    storage = get_storage()
    if not storage.configured:
        return {DELETED: 0}
    deleted, error = _delete(db, storage, time.monotonic() + settings.GCS_ROUND_SECONDS)
    _record(db, DELETE_TASK, now, error)
    return {DELETED: deleted}


def _delete(db: Session, storage: ObjectStorage, deadline: float) -> tuple[int, str | None]:
    """Vacía la cola por lotes, cada objeto en su transacción corta (ninguna abierta durante la red)."""
    repo = StorageRepository(db)
    done, after = 0, ""
    try:
        while _has_time(deadline):
            names = repo.deletions(after, settings.GCS_BATCH_SIZE)
            db.commit()
            for name in names:
                if not _has_time(deadline):
                    return done, None
                after = name
                storage.delete(name)
                repo.remove_deletion(name)
                db.commit()
                done += 1
            if len(names) < settings.GCS_BATCH_SIZE:
                break
    except Exception as exc:  # el bucket o la BD fallaron: esta tarea falla sola y sigue en otra vuelta
        db.rollback()
        logger.exception("Almacenamiento de imágenes: falló el borrado de objetos (se reintenta en otra vuelta)")
        return done, f"{type(exc).__name__}: {exc}"[:500]
    return done, None


def _record(db: Session, task: str, now: datetime, error: str | None) -> None:
    try:
        StorageRepository(db).record(task, now, error)
        db.commit()
    except Exception:  # anotar el resultado es accesorio: nunca detiene el mantenimiento
        db.rollback()
        logger.exception("No se pudo anotar el resultado de %s", task)


# ---------------------------------------------------------------- estado para el ADMIN


def status(db: Session) -> ObjectStorageStatus:
    """Configurado o no, dónde, cuántas imágenes de cada tipo hay en el bucket y la cola de borrado con su
    última vuelta. Conteos con tope y pocas consultas fijas, sin llamadas al bucket."""
    storage = get_storage()
    info = storage.describe()
    repo = StorageRepository(db)
    stored = repo.count_stored(STORED_IMAGES, LOG_COUNT_CAP)  # todos los tipos en una consulta
    images = [
        StoredImageCount(kind=image.kind, label=t(image.label), stored=count)
        for image, count in zip(STORED_IMAGES, stored, strict=True)
    ]
    deletions = _task_status(
        DELETE_TASK, t("STORAGE_PENDING_DELETIONS"), repo.count_deletions(LOG_COUNT_CAP), repo.status(DELETE_TASK)
    )
    return ObjectStorageStatus(
        configured=storage.configured,
        backend=info["backend"] or "disabled",
        bucket=info["bucket"],
        prefix=settings.storage_prefix,
        reason=info["reason"],
        count_cap=LOG_COUNT_CAP,
        images=images,
        tasks=[deletions],
    )


def _task_status(task: str, label: str, pending: int, recorded: StorageStatus | None) -> StorageTaskStatus:
    return StorageTaskStatus(
        task=task,
        label=label,
        pending=pending,
        last_run_at=recorded.last_run_at if recorded else None,
        last_success_at=recorded.last_success_at if recorded else None,
        last_error_at=recorded.last_error_at if recorded else None,
        last_error=recorded.last_error if recorded else None,
    )
