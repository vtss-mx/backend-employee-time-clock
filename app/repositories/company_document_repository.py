"""Consultas de los documentos de UNA empresa (`tenancy.company_documents`): su listado, su papelera y cada uno por id.

Siempre con la empresa en la condición (también para el ADMIN, que opera como plataforma: un id de otra empresa no se
encuentra) y por sus índices: `ix_company_documents_company` (vigentes, el más reciente primero) e
`ix_company_documents_deleted` (la papelera).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompanyDocument
from app.repositories.aggregates import get_scoped, paginate, trash_page


class CompanyDocumentRepository:
    """Documentos de una empresa (sin reglas de negocio ni `commit`)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def page(self, *, offset: int, limit: int, deleted: bool = False) -> tuple[list[CompanyDocument], int]:
        """Los vigentes, el más reciente primero (`ix_company_documents_company` hacia atrás) o, con `deleted`, su
        papelera (`ix_company_documents_deleted`)."""
        stmt = select(CompanyDocument).where(CompanyDocument.company_id == self.company_id)
        if deleted:
            return trash_page(self.db, stmt, CompanyDocument, offset=offset, limit=limit)
        return paginate(self.db, stmt, (CompanyDocument.id.desc(),), offset=offset, limit=limit)

    def get(self, document_id: int, *, include_deleted: bool = False, lock: bool = False) -> CompanyDocument | None:
        """El documento de ESTA empresa (otra empresa: None, la ruta responde 404); con `lock`, bloqueado (eliminar y
        restaurar)."""
        return get_scoped(
            self.db, CompanyDocument, document_id, self.company_id, lock=lock, include_deleted=include_deleted
        )

    def add(self, document: CompanyDocument) -> CompanyDocument:
        document.company_id = self.company_id
        self.db.add(document)
        self.db.flush()
        return document
