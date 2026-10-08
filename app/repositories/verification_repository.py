from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.orm import Session

from app.models import VerificationLog
from app.models.enums import VerificationMethod
from app.repositories.aggregates import LOG_COUNT_CAP, paginate

NEWEST_FIRST = (VerificationLog.created_at.desc(), VerificationLog.id.desc())


class VerificationLogRepository:
    """Bitácora de identificaciones: tabla de solo inserción que crece sin fin. Sus listados cuentan
    a lo más LOG_COUNT_CAP filas y la API de integración la recorre por cursor (sin OFFSET)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, log: VerificationLog) -> VerificationLog:
        self.db.add(log)
        self.db.flush()
        return log

    def page_for_employee(self, employee_id: int, *, offset: int, limit: int) -> tuple[list[VerificationLog], int]:
        """Bitácora del empleado, la más reciente primero (índice employee_id + created_at + id)."""
        stmt = select(VerificationLog).where(VerificationLog.employee_id == employee_id)
        return paginate(self.db, stmt, NEWEST_FIRST, offset=offset, limit=limit, count_cap=LOG_COUNT_CAP)

    def page_for_actor(self, user_id: int, *, offset: int, limit: int) -> tuple[list[VerificationLog], int]:
        """Intentos que hizo una cuenta (p. ej. un validador), el más reciente primero (índice
        user_id + created_at + id)."""
        stmt = select(VerificationLog).where(VerificationLog.user_id == user_id)
        return paginate(self.db, stmt, NEWEST_FIRST, offset=offset, limit=limit, count_cap=LOG_COUNT_CAP)

    def page_for_company(
        self,
        company_id: int,
        *,
        since: datetime | None,
        until: datetime | None,
        employee_id: int | None,
        success: bool | None,
        offset: int,
        limit: int,
    ) -> tuple[list[VerificationLog], int]:
        """Bitácora de UNA empresa con filtros, la más reciente primero.

        Índice (company_id, created_at, id) INCLUDE (employee_id, success): el periodo (`since`/`until`)
        se recorre directo en el orden pedido y el conteo con tope sale del índice sin leer la tabla.
        Antes se ordenaba por `id` y, con un periodo, PostgreSQL recorría la llave primaria hacia atrás
        descartando millones de filas hasta llegar a las del periodo (≈ 0.5 s con 5 M de registros)."""
        stmt = self._company_filters(company_id, since=since, until=until, employee_id=employee_id, success=success)
        return paginate(self.db, stmt, NEWEST_FIRST, offset=offset, limit=limit, count_cap=LOG_COUNT_CAP)

    def feed_for_company(
        self,
        company_id: int,
        *,
        after_id: int | None,
        settled_before: datetime,
        until: datetime | None,
        employee_id: int | None,
        success: bool | None,
        limit: int,
    ) -> list[VerificationLog]:
        """La bitácora de la empresa en orden de llegada DESPUÉS de un registro (cursor por id).

        Para sincronizar sistemas externos: sin OFFSET ni conteo (cada tramo cuesta lo mismo aunque
        haya millones de filas). Solo entra lo anterior a `settled_before`: un registro cuya
        transacción aún no confirma no puede quedar atrás del cursor y perderse.
        """
        stmt = self._company_filters(company_id, since=None, until=until, employee_id=employee_id, success=success)
        stmt = stmt.where(VerificationLog.created_at < settled_before)
        if after_id is not None:
            stmt = stmt.where(VerificationLog.id > after_id)
        return list(self.db.scalars(stmt.order_by(VerificationLog.id.asc()).limit(limit)))

    def recent_failures(
        self,
        *,
        employee_id: int | None,
        actor_id: int | None,
        methods: Iterable[VerificationMethod],
        reasons: Iterable[str],
        since: datetime,
        limit: int,
        device_hash: str | None = None,
    ) -> list[datetime]:
        """Fallos seguidos (después del último éxito) de un empleado, de un dispositivo de la API pública o de una
        cuenta en la ventana, el más reciente primero; a lo más `limit` (es todo lo que necesita el bloqueo). Cada
        sujeto va por su índice: `ix_verification_logs_employee_created`, `ix_verification_logs_device_created`
        (parcial, solo lo que vino de la API) o `ix_verification_logs_user_created`."""
        subject: ColumnElement[bool]
        if employee_id is not None:
            subject = VerificationLog.employee_id == employee_id
        elif device_hash is not None:
            subject = VerificationLog.device_hash == device_hash
        else:
            subject = VerificationLog.user_id == actor_id
        recent = [subject, VerificationLog.method.in_(list(methods)), VerificationLog.created_at >= since]
        # El orden lo da el id de la bitácora (estrictamente creciente): varias horas pueden coincidir.
        last_success = self.db.scalar(
            select(func.max(VerificationLog.id)).where(*recent, VerificationLog.success.is_(True))
        )
        query = select(VerificationLog.created_at).where(
            *recent, VerificationLog.success.is_(False), VerificationLog.reason.in_(list(reasons))
        )
        if last_success is not None:
            query = query.where(VerificationLog.id > last_success)
        return list(self.db.scalars(query.order_by(VerificationLog.id.desc()).limit(limit)))

    @staticmethod
    def _company_filters(
        company_id: int,
        *,
        since: datetime | None,
        until: datetime | None,
        employee_id: int | None,
        success: bool | None,
    ) -> Select[VerificationLog]:
        stmt = select(VerificationLog).where(VerificationLog.company_id == company_id)
        if since is not None:
            stmt = stmt.where(VerificationLog.created_at >= since)
        if until is not None:
            stmt = stmt.where(VerificationLog.created_at < until)
        if employee_id is not None:
            stmt = stmt.where(VerificationLog.employee_id == employee_id)
        if success is not None:
            stmt = stmt.where(VerificationLog.success.is_(success))
        return stmt
