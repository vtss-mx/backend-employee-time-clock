"""Bitácora del almacenamiento de imágenes en el bucket (esquema `ops`; ver `app/services/image_storage.py`).

Lo que se sube vive en la fila dueña de la imagen (`photo_object` / `photo_stored_at` del registro
facial, `object_name` / `stored_at` del comprobante): así encontrar lo pendiente o leer la imagen no
cuesta ninguna consulta extra. Aquí solo queda lo que no tiene fila dueña:

- `storage_deletions`: objetos del bucket que hay que borrar porque su fila ya no existe (se borró el
  empleado) o ya no debe conservar la imagen (registro rechazado). La escribe la misma transacción
  que borra la fila (o el mantenimiento al descartar) y el mantenimiento la vacía contra el bucket: un
  objeto nunca se queda en la nube sin que la plataforma sepa de él.
- `storage_status`: cómo le fue a cada tarea del mantenimiento con el bucket (última vuelta correcta y
  último error) para el estado del servidor del ADMIN, igual para todas las instancias.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import OPS


class StorageDeletion(Base):
    """Un objeto del bucket por borrar (la fila que lo tenía ya no lo conserva)."""

    __tablename__ = "storage_deletions"
    __table_args__ = ({"schema": OPS},)

    #: Nombre completo del objeto (solo ids: nunca datos personales). La llave primaria evita duplicados y
    #: da el orden en que el mantenimiento los toma.
    object_name: Mapped[str] = mapped_column(String(300), primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StorageStatus(Base):
    """Resultado de una tarea del almacenamiento (subir cada tipo de imagen, borrar del bucket)."""

    __tablename__ = "storage_status"
    __table_args__ = ({"schema": OPS},)

    task: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Tipo y texto de la falla (sin contenido de imágenes ni secretos).
    last_error: Mapped[str | None] = mapped_column(String(500))
