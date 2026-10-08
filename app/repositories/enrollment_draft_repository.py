"""Consultas del borrador del registro facial (la foto inicial del paso 1; `models/enrollment_draft.py`)."""

from datetime import datetime

from sqlalchemy import ColumnElement, and_, delete, select
from sqlalchemy.orm import Session

from app.models import FaceEnrollmentDraft
from app.repositories.aggregates import affected_rows
from app.repositories.storage_repository import StorageRepository


class FaceEnrollmentDraftRepository:
    """Borradores del registro facial de UNA empresa (`company_id` en cada consulta)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def _of(self, employee_id: int) -> ColumnElement[bool]:
        """El borrador del empleado (único `(company_id, employee_id)`: su índice)."""
        return and_(FaceEnrollmentDraft.company_id == self.company_id, FaceEnrollmentDraft.employee_id == employee_id)

    def of_employee(self, employee_id: int) -> FaceEnrollmentDraft | None:
        """El borrador del empleado, vigente o vencido (a lo más uno)."""
        return self.db.scalar(select(FaceEnrollmentDraft).where(self._of(employee_id)))

    def replace(self, employee_id: int, draft: FaceEnrollmentDraft, now: datetime) -> FaceEnrollmentDraft:
        """Guarda el borrador nuevo en lugar del anterior del empleado (si lo había: su objeto a la cola del bucket y su
        fila fuera, en UNA sentencia cada uno, sin cargarla), en la misma transacción."""
        self.discard(employee_id, now)
        draft.company_id = self.company_id
        draft.employee_id = employee_id
        self.db.add(draft)
        self.db.flush()
        return draft

    def take(self, draft_id: int) -> FaceEnrollmentDraft | None:
        """El borrador pasa al registro (las capturas se aceptaron): su fila sale en UNA sentencia atómica y devuelve
        la referencia de su foto, que ahora es del registro (no va a la cola del bucket). None si ya no está (otra
        petición lo reemplazó o lo consumió mientras se analizaban las capturas)."""
        stmt = (
            delete(FaceEnrollmentDraft)
            .where(FaceEnrollmentDraft.id == draft_id, FaceEnrollmentDraft.company_id == self.company_id)
            .returning(FaceEnrollmentDraft)
            .execution_options(synchronize_session=False)
        )
        return self.db.scalar(stmt)

    def discard(self, employee_id: int, now: datetime) -> int:
        """El borrador del empleado deja de existir («Repetir foto»): su objeto del bucket a la cola de borrado y su
        fila fuera. Cuenta las filas borradas."""
        condition = self._of(employee_id)
        StorageRepository(self.db).enqueue_from(FaceEnrollmentDraft.photo_object, condition, now)
        return affected_rows(self.db, delete(FaceEnrollmentDraft).where(condition))
