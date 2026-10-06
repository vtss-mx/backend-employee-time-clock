"""Bandeja de errores del sistema para el ADMIN de la plataforma: consultar y dar seguimiento.

Las fallas las registra `error_reporter` (solo lo que alguien debe corregir: fallas del servidor, del
segundo plano, del canal en vivo y de la aplicación web; un 4xx no). Aquí
el ADMIN los revisa y los marca: pendiente, en proceso, en revisión o solucionado. Un solucionado que
vuelve a ocurrir se reabre solo.
"""

import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, UnprocessableError
from app.i18n import t
from app.models import ErrorReport, ErrorStatus, User, UserRole
from app.repositories.company_repository import CompanyRepository
from app.repositories.error_report_repository import ErrorReportRepository
from app.repositories.user_repository import UserRepository
from app.schemas.common import PageParams
from app.schemas.error_report import (
    ErrorBulkResolve,
    ErrorOccurrenceList,
    ErrorOccurrenceRead,
    ErrorReportDetail,
    ErrorReportList,
    ErrorReportRead,
    ErrorSummary,
)
from app.services.catalog_service import get_catalogs


class ErrorReportService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ErrorReportRepository(db)

    def list_reports(
        self, *, status: str | None, severity: str | None, search: str | None, page: PageParams
    ) -> ErrorReportList:
        as_of = datetime.now(UTC)  # antes de consultar: nada de lo listado ocurrió después
        items, total = self.repo.search(
            status=status, severity=severity, search=search, offset=page.offset, limit=page.size
        )
        emails = UserRepository(self.db).emails_by_ids(r.status_changed_by_id for r in items)
        return ErrorReportList.of([self._read(r, emails) for r in items], total, page, as_of=as_of)

    def detail(self, report_id: int) -> ErrorReportDetail:
        report = self._get(report_id)
        emails = UserRepository(self.db).emails_by_ids((report.status_changed_by_id,))
        return ErrorReportDetail(**self._read(report, emails).model_dump(), detail=report.detail)

    def occurrences(self, report_id: int, page: PageParams) -> ErrorOccurrenceList:
        self._get(report_id)
        items, total = self.repo.occurrences(report_id, offset=page.offset, limit=page.size)
        accounts = UserRepository(self.db).accounts_by_ids(o.user_id for o in items)
        companies = CompanyRepository(self.db).names_by_ids(o.company_id for o in items if o.company_id)
        return ErrorOccurrenceList.of(
            [
                ErrorOccurrenceRead(
                    id=o.id,
                    occurred_at=o.occurred_at,
                    trace_id=o.trace_id,
                    message=o.message,
                    user_label=self._account_label(o.user_id, accounts),
                    company_name=companies.get(o.company_id) if o.company_id else None,
                    context=json.loads(o.context) if o.context else None,
                )
                for o in items
            ],
            total,
            page,
        )

    def summary(self) -> ErrorSummary:
        by_status, open_by_severity = self.repo.counts()
        return ErrorSummary(
            by_status=by_status,
            open_by_severity=open_by_severity,
            pending=by_status.get(ErrorStatus.PENDING, 0),
            last_seen_at=self.repo.latest_seen(),
        )

    def set_status(self, report_id: int, status: ErrorStatus, admin: User) -> ErrorReportDetail:
        """El ADMIN marca el seguimiento; queda quién y cuándo (fila bloqueada: dos cambios a la vez
        no se pisan)."""
        report = self._get(report_id, for_update=True)
        if report.status != status:
            report.status = status
            report.status_changed_at = datetime.now(UTC)
            report.status_changed_by_id = admin.id
        self.db.commit()
        return self.detail(report.id)

    def resolve_matching(self, data: ErrorBulkResolve, admin: User) -> int:
        """Marca como solucionados, de una vez, los errores abiertos del filtro elegido. Exige un estado
        o una gravedad específicos (nunca toda la bandeja) y respeta lo que el ADMIN vio: lo que volvió
        a ocurrir después de `seen_until` sigue abierto. Devuelve cuántos cambiaron."""
        if data.status is None and data.severity is None:
            raise UnprocessableError(code="ERROR_FILTER_REQUIRED")
        if data.status == ErrorStatus.RESOLVED:
            raise UnprocessableError(code="ERROR_FILTER_RESOLVED", field="status")
        now = datetime.now(UTC)
        resolved = self.repo.resolve_matching(
            status=data.status,
            severity=data.severity,
            search=data.search,
            seen_until=min(data.seen_until, now),
            admin_id=admin.id,
            now=now,
        )
        self.db.commit()
        return resolved

    # ---------- Internos ----------

    def _get(self, report_id: int, *, for_update: bool = False) -> ErrorReport:
        report = self.repo.get(report_id, for_update=for_update)
        if report is None:
            raise NotFoundError(code="ERROR_REPORT_NOT_FOUND")
        return report

    @staticmethod
    def _account_label(user_id: int | None, accounts: dict[int, tuple[str, UserRole]]) -> str | None:
        """Quién fue, tal cual: «correo (Rol)»; si la cuenta ya no existe, su número."""
        if user_id is None:
            return None
        account = accounts.get(user_id)
        if account is None:
            return t("ACCOUNT_DELETED_LABEL", {"id": user_id})
        email, role = account
        return t("ACCOUNT_LABEL", {"email": email, "role": get_catalogs().name("roles", role)})

    @staticmethod
    def _read(report: ErrorReport, emails: dict[int, str]) -> ErrorReportRead:
        read = ErrorReportRead.model_validate(report)
        return read.model_copy(update={"status_changed_by": emails.get(report.status_changed_by_id or 0)})
