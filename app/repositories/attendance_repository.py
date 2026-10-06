"""Consultas de la asistencia por turno de UNA empresa: jornadas, descansos y la bitácora de registros.

La jornada abierta se lee con candado de fila (`FOR UPDATE`): dos registros simultáneos del mismo
empleado se atienden uno tras otro y el segundo ve lo que hizo el primero. Los índices únicos de la
base (una jornada abierta por empleado, un descanso abierto por jornada) cierran cualquier carrera
que quedara.
"""

from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.orm import Session

from app.core.soft_delete import with_deleted
from app.models import (
    AttendanceEvent,
    User,
    Validator,
    VerificationLog,
    WorkBreak,
    WorkSession,
    WorkSessionStatus,
    WorkSite,
)
from app.repositories.aggregates import LOG_COUNT_CAP, affected_rows, insert_many, paginate

#: Registro "en revisión" que espera la decisión de la empresa (catalog.attendance_review_statuses).
REVIEW_PENDING = "PENDING"


class AttendanceRepository:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    # ---------- Jornadas ----------

    def open_session(self, employee_id: int, *, lock: bool = False) -> WorkSession | None:
        stmt = select(WorkSession).where(
            WorkSession.company_id == self.company_id,
            WorkSession.employee_id == employee_id,
            WorkSession.status == WorkSessionStatus.OPEN,
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return self.db.scalar(stmt)

    def session_at(self, employee_id: int, scheduled_start: datetime) -> WorkSession | None:
        """La jornada de ese turno (ya registrada o no)."""
        stmt = select(WorkSession).where(
            WorkSession.company_id == self.company_id,
            WorkSession.employee_id == employee_id,
            WorkSession.scheduled_start == scheduled_start,
        )
        return self.db.scalar(stmt)

    def session(self, session_id: int) -> WorkSession | None:
        session = self.db.get(WorkSession, session_id)
        return session if session is not None and session.company_id == self.company_id else None

    def add(self, record: WorkSession | WorkBreak | AttendanceEvent) -> None:
        record.company_id = self.company_id
        self.db.add(record)
        self.db.flush()

    def add_all[T: (WorkBreak, AttendanceEvent)](self, records: list[T]) -> None:
        """Varios descansos o registros en UNA inserción (la jornada que registra o corrige la empresa):
        `insert_many`, porque el flush del ORM inserta uno por uno donde la base no garantiza el orden de
        RETURNING (SQLite) y el costo crecería con cada descanso declarado."""
        for record in records:
            record.company_id = self.company_id
        insert_many(self.db, records)

    def delete_breaks(self, session_id: int) -> None:
        """Los descansos de la jornada (la empresa la corrige con los que declara): una sentencia."""
        affected_rows(
            self.db,
            delete(WorkBreak).where(WorkBreak.company_id == self.company_id, WorkBreak.session_id == session_id),
        )

    def history(
        self,
        *,
        employee_id: int | None,
        start: date | None,
        end: date | None,
        status: str | None,
        offset: int,
        limit: int,
        in_review: bool = False,
    ) -> tuple[list[WorkSession], int]:
        stmt = select(WorkSession).where(WorkSession.company_id == self.company_id)
        if in_review:
            # La bandeja "en revisión": el índice parcial (company_id, work_date, scheduled_start, id) WHERE
            # review_status = 'PENDING' entrega el mismo orden sin leer las demás jornadas.
            stmt = stmt.where(WorkSession.review_status == REVIEW_PENDING)
        if employee_id is not None:
            stmt = stmt.where(WorkSession.employee_id == employee_id)
        if start is not None:
            stmt = stmt.where(WorkSession.work_date >= start)
        if end is not None:
            stmt = stmt.where(WorkSession.work_date <= end)
        if status is not None:
            stmt = stmt.where(WorkSession.status == status)
        # El índice (company_id, work_date, scheduled_start, id) entrega este orden sin ordenar. Las
        # jornadas crecen sin fin (una por empleado y día): el total se cuenta con tope, como la bitácora.
        order = (WorkSession.work_date.desc(), WorkSession.scheduled_start.desc(), WorkSession.id.desc())
        return paginate(self.db, stmt, order, offset=offset, limit=limit, count_cap=LOG_COUNT_CAP)

    def pending_reviews(self, cap: int) -> int:
        """Jornadas "en revisión" de la empresa (contador del menú), contadas hasta `cap` en el índice parcial."""
        inner = (
            select(WorkSession.id)
            .where(WorkSession.company_id == self.company_id, WorkSession.review_status == REVIEW_PENDING)
            .limit(cap)
            .subquery()
        )
        return int(self.db.scalar(select(func.count()).select_from(inner)) or 0)

    def sessions_on(self, employee_ids: Iterable[int], work_date: date) -> dict[int, WorkSession]:
        """Jornada de cada empleado ese día (tablero): una consulta para toda la página."""
        ids = list(employee_ids)
        if not ids:
            return {}
        stmt = select(WorkSession).where(
            WorkSession.company_id == self.company_id,
            WorkSession.employee_id.in_(ids),
            WorkSession.work_date == work_date,
        )
        return {session.employee_id: session for session in self.db.scalars(stmt)}

    def day_counts(self, work_date: date) -> dict[str, tuple[int, int]]:
        """{estado: (jornadas, jornadas con un descanso en curso)} de un día de toda la empresa (conteos
        del tablero) en UN GROUP BY.

        Antes se cargaban todas las jornadas del día para contarlas en Python: una empresa con miles de
        empleados armaba miles de objetos en cada refresco del tablero (una lista sin límite). Ahora se
        cuentan en la base: las jornadas salen del índice (company_id, work_date, ...) INCLUDE (status)
        y el descanso abierto, del índice único parcial `uq_work_breaks_session_open` (a lo más uno por
        jornada, así que el LEFT JOIN no duplica filas)."""
        stmt = (
            select(WorkSession.status, func.count(), func.count(WorkBreak.id))
            .outerjoin(WorkBreak, and_(WorkBreak.session_id == WorkSession.id, WorkBreak.ended_at.is_(None)))
            .where(WorkSession.company_id == self.company_id, WorkSession.work_date == work_date)
            .group_by(WorkSession.status)
        )
        return {str(status): (int(total), int(on_break)) for status, total, on_break in self.db.execute(stmt)}

    # ---------- Descansos ----------

    def breaks_of(self, session_ids: Iterable[int]) -> dict[int, list[WorkBreak]]:
        ids = list(session_ids)
        if not ids:
            return {}
        stmt = (
            select(WorkBreak)
            .where(WorkBreak.company_id == self.company_id, WorkBreak.session_id.in_(ids))
            .order_by(WorkBreak.id)
        )
        found: dict[int, list[WorkBreak]] = {session_id: [] for session_id in ids}
        for item in self.db.scalars(stmt):
            found[item.session_id].append(item)
        return found

    # ---------- Bitácora ----------

    def last_located_event(self, employee_id: int, since: datetime) -> AttendanceEvent | None:
        """El último registro del empleado con ubicación desde `since` (para detectar un viaje imposible).

        `since` acota la búsqueda a los meses en que un viaje aún podría ser imposible (§3.1.5): la bitácora está
        particionada por mes en `occurred_at`, así se leen solo esas particiones, en el orden del índice
        `(employee_id, occurred_at, id)`, en lugar de la historia completa del empleado."""
        stmt = (
            select(AttendanceEvent)
            .where(
                AttendanceEvent.company_id == self.company_id,
                AttendanceEvent.employee_id == employee_id,
                AttendanceEvent.occurred_at >= since,
                AttendanceEvent.latitude.is_not(None),
            )
            .order_by(AttendanceEvent.occurred_at.desc(), AttendanceEvent.id.desc())
        )
        return self.db.scalar(stmt.limit(1))

    def events_of(self, session_id: int) -> list[tuple[AttendanceEvent, float | None, str | None]]:
        """Cada registro de la jornada con la confianza de su verificación y quién operó (historial: también un
        validador o una cuenta que ya está en «Eliminados»)."""
        operator = func.coalesce(Validator.name, User.email)
        stmt = (
            select(AttendanceEvent, VerificationLog.score, operator)
            .outerjoin(VerificationLog, VerificationLog.id == AttendanceEvent.verification_log_id)
            .outerjoin(User, User.id == AttendanceEvent.actor_id)
            .outerjoin(
                Validator, and_(Validator.user_id == AttendanceEvent.actor_id, Validator.company_id == self.company_id)
            )
            .where(AttendanceEvent.company_id == self.company_id, AttendanceEvent.session_id == session_id)
            .order_by(AttendanceEvent.id)
        )
        return [(event, score, name) for event, score, name in self.db.execute(with_deleted(stmt))]

    def site_names(self, site_ids: Iterable[int | None]) -> dict[int, str]:
        """Nombre de los sitios donde se checó (historial: también los que están en «Eliminados»)."""
        ids = list({site_id for site_id in site_ids if site_id is not None})
        if not ids:
            return {}
        stmt = select(WorkSite.id, WorkSite.name).where(WorkSite.company_id == self.company_id, WorkSite.id.in_(ids))
        return dict(self.db.execute(with_deleted(stmt)).all())  # filas (id, nombre)


def close_missed_checkouts(db: Session, now: datetime) -> int:
    """Mantenimiento (todas las empresas): las jornadas abiertas cuyo límite de salida venció quedan
    "sin salida" y su descanso abierto termina en ese límite. Dos sentencias, sin ciclo por fila."""
    expired = select(WorkSession.id).where(
        WorkSession.status == WorkSessionStatus.OPEN, WorkSession.check_out_deadline < now
    )
    deadline = select(WorkSession.check_out_deadline).where(WorkSession.id == WorkBreak.session_id).scalar_subquery()
    affected_rows(
        db,
        update(WorkBreak)
        .where(WorkBreak.ended_at.is_(None), WorkBreak.session_id.in_(expired))
        .values(ended_at=deadline),
    )
    return affected_rows(
        db,
        update(WorkSession)
        .where(WorkSession.status == WorkSessionStatus.OPEN, WorkSession.check_out_deadline < now)
        .values(status=WorkSessionStatus.MISSED_CHECKOUT),
    )
