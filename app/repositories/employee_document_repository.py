"""Consultas de los documentos de un empleado (`workforce.employee_documents`): su listado, su papelera, cada uno por
id y los tipos que ya tiene (para saber qué falta en el onboarding).

Siempre con la empresa Y el empleado en la condición (la empresa sale de la sesión, el empleado del id del dueño o del
expediente que revisa la empresa) y por el índice `ix_employee_documents_employee` (`company_id, employee_id, id`:
vigentes el más reciente primero) o `ix_employee_documents_deleted` (la papelera).
"""

from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.soft_delete import WITH_DELETED
from app.models import EmployeeDocument
from app.repositories.aggregates import affected_rows, paginate, trash_page


class EmployeeDocumentRepository:
    """Documentos de un empleado dentro de UNA empresa (sin reglas de negocio ni `commit`)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def _scoped(self, employee_id: int) -> Any:
        return select(EmployeeDocument).where(
            EmployeeDocument.company_id == self.company_id, EmployeeDocument.employee_id == employee_id
        )

    def page(
        self, employee_id: int, *, offset: int, limit: int, deleted: bool = False
    ) -> tuple[list[EmployeeDocument], int]:
        """Los vigentes del empleado, el más reciente primero, o su papelera (`deleted`)."""
        stmt = self._scoped(employee_id)
        if deleted:
            return trash_page(self.db, stmt, EmployeeDocument, offset=offset, limit=limit)
        return paginate(self.db, stmt, (EmployeeDocument.id.desc(),), offset=offset, limit=limit)

    def get(
        self, document_id: int, employee_id: int, *, include_deleted: bool = False, lock: bool = False
    ) -> EmployeeDocument | None:
        """El documento de ESTE empleado y ESTA empresa (otra: None → 404); con `lock`, bloqueado (editar, eliminar y
        restaurar). La empresa y el empleado van en el WHERE: nunca se bloquea una fila ajena con un id del cliente."""
        stmt = self._scoped(employee_id).where(EmployeeDocument.id == document_id)
        options = WITH_DELETED if include_deleted else {}
        if lock:
            stmt = stmt.with_for_update(of=EmployeeDocument).execution_options(populate_existing=True, **options)
        elif include_deleted:
            stmt = stmt.execution_options(**options)
        return cast(EmployeeDocument | None, self.db.scalar(stmt))

    def live_types(self, employee_id: int) -> set[str]:
        """Los tipos de documento VIGENTES que el empleado ya tiene (para calcular qué le falta subir)."""
        stmt = self._scoped(employee_id).with_only_columns(EmployeeDocument.type).distinct()
        return set(self.db.scalars(stmt))

    def add(self, document: EmployeeDocument) -> EmployeeDocument:
        document.company_id = self.company_id
        self.db.add(document)
        self.db.flush()
        return document

    def erase_employee(self, employee_id: int) -> None:
        """Borrado DE VERDAD de los documentos de identidad de un empleado que se elimina (regla 13, LFPDPPP: una
        identificación lleva la foto y los datos de la persona): UNA sentencia que borra TODAS sus filas —vigentes y
        las que él mismo había eliminado de su lista— por el índice `ix_employee_documents_employee`. Un DELETE no lo
        filtra el borrado lógico, así que se lleva también lo que estaba en «Eliminados». Sus objetos del bucket ya se
        encolaron antes (`image_storage.release_employee_documents`); su ficha y su historial de empleo se conservan
        (el empleado es un borrado lógico)."""
        stmt = delete(EmployeeDocument).where(
            EmployeeDocument.company_id == self.company_id, EmployeeDocument.employee_id == employee_id
        )
        affected_rows(self.db, stmt)
