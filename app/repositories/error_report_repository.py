import json
from datetime import datetime

from sqlalchemy import ColumnElement, case, func, or_, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.core.error_events import CONTEXT_LIMIT, DETAIL_LIMIT, ErrorEvent, clip
from app.models import ErrorOccurrence, ErrorReport
from app.repositories.aggregates import affected_rows, paginate


class ErrorReportRepository:
    """Errores del sistema (esquema ops): guardado agrupado y bandeja del ADMIN."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------- Guardado (hilo del registro de errores) ----------

    def record(self, fingerprint: str, events: list[ErrorEvent], keep: int) -> None:
        """Un error (sus ocurrencias iguales): UPSERT que suma ocurrencias y sus `keep` más recientes.

        Un error SOLUCIONADO que vuelve a ocurrir se reabre como pendiente y cuenta la reapertura: el
        ADMIN se entera de que la corrección no bastó. Todo se guarda literal (sin caracteres
        inválidos y dentro de su columna), con el contexto de cada ocurrencia."""
        ordered = sorted(events, key=lambda e: e.occurred_at)
        report_id = self._upsert(fingerprint, ordered)
        self.db.add_all(
            ErrorOccurrence(
                report_id=report_id,
                occurred_at=event.occurred_at,
                trace_id=clip(event.trace_id, 64),
                user_id=event.user_id,
                company_id=event.company_id,
                message=clip(event.message, 1000) or "-",
                context=clip(json.dumps(event.context, ensure_ascii=False, default=str), CONTEXT_LIMIT)
                if event.context
                else None,
            )
            for event in ordered[-keep:]
        )
        self.db.flush()

    def _upsert(self, fingerprint: str, ordered: list[ErrorEvent]) -> int:
        first, last = ordered[0], ordered[-1]
        detail = next((e.detail for e in reversed(ordered) if e.detail), None)
        dialect = postgresql if self.db.get_bind().dialect.name == "postgresql" else sqlite
        stmt = dialect.insert(ErrorReport).values(
            fingerprint=fingerprint,
            source=last.source,
            severity=last.severity,
            status="PENDING",
            code=clip(last.code, 120) or "-",
            message=clip(last.message, 1000) or "-",
            http_status=last.http_status,
            method=clip(last.method, 10),
            location=clip(last.location, 255),
            exception_type=clip(last.exception_type, 255),
            detail=clip(detail, DETAIL_LIMIT),
            occurrences=len(ordered),
            reopened=0,
            first_seen_at=first.occurred_at,
            last_seen_at=last.occurred_at,
            last_trace_id=clip(last.trace_id, 64),
        )
        resolved = ErrorReport.status == "RESOLVED"
        stmt = stmt.on_conflict_do_update(
            index_elements=[ErrorReport.fingerprint],
            set_={
                "occurrences": ErrorReport.occurrences + stmt.excluded.occurrences,
                "last_seen_at": stmt.excluded.last_seen_at,
                "last_trace_id": stmt.excluded.last_trace_id,
                "message": stmt.excluded.message,
                "detail": func.coalesce(stmt.excluded.detail, ErrorReport.detail),
                "status": case((resolved, "PENDING"), else_=ErrorReport.status),
                "reopened": ErrorReport.reopened + case((resolved, 1), else_=0),
            },
        ).returning(ErrorReport.id)
        return int(self.db.execute(stmt).scalar_one())

    # ---------- Bandeja del ADMIN ----------

    def search(
        self, *, status: str | None, severity: str | None, search: str | None, offset: int, limit: int
    ) -> tuple[list[ErrorReport], int]:
        stmt = select(ErrorReport).where(*_filters(status, severity, search))
        return paginate(
            self.db, stmt, (ErrorReport.last_seen_at.desc(), ErrorReport.id.desc()), offset=offset, limit=limit
        )

    def resolve_matching(
        self,
        *,
        status: str | None,
        severity: str | None,
        search: str | None,
        seen_until: datetime,
        admin_id: int,
        now: datetime,
    ) -> int:
        """Marca como solucionados los errores del filtro que siguen abiertos y no han vuelto a ocurrir
        desde `seen_until`: una sentencia (atómica frente a una ocurrencia nueva que los reabre)."""
        stmt = (
            update(ErrorReport)
            .where(
                *_filters(status, severity, search),
                ErrorReport.status != "RESOLVED",
                ErrorReport.last_seen_at <= seen_until,
            )
            .values(status="RESOLVED", status_changed_at=now, status_changed_by_id=admin_id)
        )
        return affected_rows(self.db, stmt)

    def get(self, report_id: int, *, for_update: bool = False) -> ErrorReport | None:
        return self.db.get(ErrorReport, report_id, with_for_update=for_update or None, populate_existing=for_update)

    def occurrences(self, report_id: int, *, offset: int, limit: int) -> tuple[list[ErrorOccurrence], int]:
        stmt = select(ErrorOccurrence).where(ErrorOccurrence.report_id == report_id)
        return paginate(self.db, stmt, (ErrorOccurrence.id.desc(),), offset=offset, limit=limit)

    def counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """Errores por estado y, de los que siguen abiertos, por gravedad (contador del menú y filtros)."""
        by_status = dict(self.db.execute(select(ErrorReport.status, func.count()).group_by(ErrorReport.status)).all())
        open_ = select(ErrorReport.severity, func.count()).where(ErrorReport.status != "RESOLVED")
        by_severity = dict(self.db.execute(open_.group_by(ErrorReport.severity)).all())
        return {str(k): int(v) for k, v in by_status.items()}, {str(k): int(v) for k, v in by_severity.items()}

    def latest_seen(self) -> datetime | None:
        return self.db.scalar(select(func.max(ErrorReport.last_seen_at)))


def _filters(status: str | None, severity: str | None, search: str | None) -> list[ColumnElement[bool]]:
    """Las condiciones del filtro de la bandeja (las mismas al listar y al marcar en bloque)."""
    conditions = []
    if status:
        conditions.append(ErrorReport.status == status)
    if severity:
        conditions.append(ErrorReport.severity == severity)
    term = " ".join((search or "").split()).lower()
    if term:
        conditions.append(_matches(term))
    return conditions


def _matches(term: str) -> ColumnElement[bool]:
    """Búsqueda por código, mensaje, ruta o tipo de excepción (sin distinguir mayúsculas)."""
    return or_(
        *(
            func.lower(column).contains(term, autoescape=True)
            for column in (ErrorReport.code, ErrorReport.message, ErrorReport.location, ErrorReport.exception_type)
        )
    )
