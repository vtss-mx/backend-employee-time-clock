"""Estado del almacenamiento de imágenes en el bucket, para el ADMIN (estado del servidor)."""

from datetime import datetime

from pydantic import BaseModel


class StoredImageCount(BaseModel):
    """Un tipo de imagen o archivo (`STORED_IMAGES`) y cuántos hay en el bucket. Ninguno vive en la BD."""

    kind: str
    label: str
    #: Objetos en el bucket (con tope `count_cap`).
    stored: int


class StorageTaskStatus(BaseModel):
    """Una tarea del bucket en segundo plano (hoy `delete`: vaciar la cola de borrado), con lo que tiene
    pendiente y su última vuelta del mantenimiento."""

    task: str
    label: str
    #: Objetos en la cola (con tope `count_cap`).
    pending: int
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None


class ObjectStorageStatus(BaseModel):
    """Si el bucket está configurado, dónde guarda, qué hay en él y cómo van sus tareas. Nunca la llave."""

    configured: bool
    #: gcs | disabled
    backend: str
    bucket: str | None = None
    #: Carpeta de este entorno dentro del bucket.
    prefix: str
    #: Por qué está apagado (sin bucket, sin llave, llave inválida).
    reason: str | None = None
    #: Los conteos llegan a lo más a este número ("10 000+").
    count_cap: int
    images: list[StoredImageCount]
    tasks: list[StorageTaskStatus]
