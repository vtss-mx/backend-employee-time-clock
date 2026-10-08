"""Consultas del almacenamiento de imágenes en el bucket (`app/services/image_storage.py`).

Genéricas por tipo de imagen (`StoredImage` del registro `STORED_IMAGES`): cada tipo dice qué fila guarda
la referencia de su objeto (sus bytes nunca están en la BD). Así un tipo nuevo no escribe SQL nuevo.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, func, literal, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.soft_delete import with_deleted
from app.models import StorageDeletion, StorageStatus
from app.repositories.aggregates import affected_rows, dialect_insert


@dataclass(frozen=True)
class ImageRef:
    """Lo que hace falta de una fila para nombrar el objeto de su imagen (solo ids, nunca datos personales)."""

    key: int
    #: Empresa dueña (carpeta `companies/<empresa>/`); None en lo que es de la persona y no de una empresa
    #: (la foto de perfil, `people/users/<cuenta>/`).
    company_id: int | None
    content_type: str | None
    #: Valores de `StoredImage.extra` (p. ej. el empleado del registro facial), en su orden.
    extra: tuple[Any, ...] = ()


@dataclass(frozen=True)
class StoredImage:
    """Un tipo de imagen o archivo de la plataforma: su fila dueña, las columnas de la referencia de su
    objeto CIFRADO en el bucket y cómo se nombra. El registro de todos es `STORED_IMAGES`."""

    #: Identificador estable (metadato del objeto y su renglón en el estado del ADMIN).
    kind: str
    #: Llave de su nombre en el catálogo de mensajes (el estado del ADMIN lo muestra en el idioma de quien lo lee).
    label: str
    #: Tabla dueña de la referencia.
    model: type[Any]
    key: InstrumentedAttribute[Any]
    #: Columna de la empresa dueña; None si la imagen es de la persona (no de una empresa).
    company: InstrumentedAttribute[Any] | None
    content_type: InstrumentedAttribute[Any]
    #: La referencia: nombre del objeto, tamaño del archivo original, SHA-256 del objeto cifrado y cuándo
    #: se subió (verificado).
    object_name: InstrumentedAttribute[Any]
    size: InstrumentedAttribute[Any]
    sha256: InstrumentedAttribute[Any]
    uploaded_at: InstrumentedAttribute[Any]
    #: Ruta del objeto dentro de la carpeta del entorno (solo ids).
    path: Callable[[ImageRef], str]
    #: Columnas que también necesita `path` (en su orden, en `ImageRef.extra`).
    extra: tuple[InstrumentedAttribute[Any], ...] = ()

    @property
    def reference_columns(self) -> tuple[InstrumentedAttribute[Any], ...]:
        return (self.object_name, self.size, self.sha256, self.uploaded_at)


class StorageRepository:
    """SQL del almacenamiento de imágenes (sin reglas de negocio ni `commit`)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------ conteos del estado del ADMIN

    def count_stored(self, images: Sequence[StoredImage], cap: int) -> list[int]:
        """Objetos de cada tipo en el bucket (filas con referencia), cada uno contado hasta `cap`, en UNA consulta (un
        subconteo con tope por tipo, en el orden de `images`): un tipo nuevo en `STORED_IMAGES` no agrega consultas al
        estado del ADMIN. También los de una fila en «Eliminados» (borrado lógico): su objeto sigue en el bucket hasta
        que la depuración borra la fila."""
        counts = [
            select(func.count())
            .select_from(select(image.key).where(image.object_name.is_not(None)).limit(cap).subquery())
            .scalar_subquery()
            for image in images
        ]
        return [int(count or 0) for count in self.db.execute(with_deleted(select(*counts))).one()]

    # ------------------------------------------------------------------ cola de borrados del bucket

    def enqueue(self, name: str, now: datetime) -> None:
        """Agrega un objeto a la cola de borrado (uno ya encolado no se repite)."""
        stmt = dialect_insert(self.db, StorageDeletion).values(object_name=name, requested_at=now)
        self.db.execute(stmt.on_conflict_do_nothing(index_elements=["object_name"]))

    def enqueue_many(self, names: list[str], now: datetime) -> None:
        """Encola en UNA sentencia los objetos de filas que se acaban de borrar (la depuración de lo eliminado los
        recibe del `DELETE ... RETURNING`: misma transacción, así solo sale del bucket lo que de verdad se borró)."""
        if not names:
            return
        rows = [{"object_name": name, "requested_at": now} for name in names]
        stmt = dialect_insert(self.db, StorageDeletion).values(rows)
        self.db.execute(stmt.on_conflict_do_nothing(index_elements=["object_name"]))

    def enqueue_from(self, column: InstrumentedAttribute[Any], condition: ColumnElement[bool], now: datetime) -> None:
        """Encola en UNA sentencia los objetos de las filas que dejan de conservarlos (misma transacción
        que el cambio: si se revierte, la cola también)."""
        rows = select(column, literal(now)).where(condition, column.is_not(None))
        stmt = dialect_insert(self.db, StorageDeletion).from_select(["object_name", "requested_at"], rows)
        self.db.execute(stmt.on_conflict_do_nothing(index_elements=["object_name"]))

    def deletions(self, after: str, limit: int) -> list[str]:
        stmt = (
            select(StorageDeletion.object_name)
            .where(StorageDeletion.object_name > after)
            .order_by(StorageDeletion.object_name)
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def remove_deletion(self, name: str) -> None:
        affected_rows(self.db, delete(StorageDeletion).where(StorageDeletion.object_name == name))

    def count_deletions(self, cap: int) -> int:
        """Objetos en la cola de borrado, contados hasta `cap`."""
        count = select(func.count()).select_from(select(StorageDeletion.object_name).limit(cap).subquery())
        return int(self.db.scalar(count) or 0)

    # ------------------------------------------------------------------ estado de las tareas

    def record(self, task: str, now: datetime, error: str | None) -> None:
        """Anota cómo le fue a una tarea (una fila por tarea, igual para todas las instancias). Un error
        conserva la última vuelta correcta y viceversa."""
        values: dict[str, Any] = {"task": task, "last_run_at": now}
        if error is None:
            values["last_success_at"] = now
        else:
            values |= {"last_error_at": now, "last_error": error[:500]}
        stmt = dialect_insert(self.db, StorageStatus).values(values)
        changed = {key: getattr(stmt.excluded, key) for key in values if key != "task"}
        self.db.execute(stmt.on_conflict_do_update(index_elements=["task"], set_=changed))

    def status(self, task: str) -> StorageStatus | None:
        return self.db.get(StorageStatus, task)
