"""Reglas comunes de «Eliminados» (borrado lógico, regla 20 de la raíz; mecanismo en `app/core/soft_delete.py`).

Cada servicio que elimina y restaura usa estas piezas, así los códigos y los mensajes son los mismos en todas las
pantallas: eliminar lo que ya está en la papelera responde 409 `ALREADY_DELETED`, restaurar lo que no está 409
`NOT_DELETED`, y si al restaurar otro registro vigente ya tomó su dato único, 409 `RESTORE_CONFLICT` con el campo (el
índice único parcial es la última barrera: un choque al confirmar también lo responde).
"""

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.core.soft_delete import SoftDeleteMixin


def ensure_live(record: SoftDeleteMixin) -> None:
    """Solo se elimina lo vigente (409 `ALREADY_DELETED` si ya estaba en «Eliminados»)."""
    if record.deleted:
        raise ConflictError(code="ALREADY_DELETED")


def ensure_deleted(record: SoftDeleteMixin) -> datetime:
    """Solo se restaura lo que está en «Eliminados» (409 `NOT_DELETED`); devuelve cuándo se eliminó (lo que se
    eliminó junto con él lleva la misma marca)."""
    if record.deleted_at is None:
        raise ConflictError(code="NOT_DELETED")
    return record.deleted_at


def ensure_name_free(taken: bool, name: str) -> None:
    """Al restaurar: otro registro vigente ya tiene su nombre (departamento, sitio o turno)."""
    if taken:
        raise ConflictError(code="RESTORE_CONFLICT", key="RESTORE_NAME_TAKEN", params={"name": name}, field="name")


def commit_restore(db: Session) -> None:
    """Confirma una restauración; si otra petición tomó el mismo dato único al mismo tiempo (el índice parcial lo
    rechaza), 409 `RESTORE_CONFLICT` y nada cambia."""
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(code="RESTORE_CONFLICT") from exc
