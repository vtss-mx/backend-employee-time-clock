"""Lo que la empresa ve de la asistencia: el tablero de un día, el historial y la evidencia de cada jornada.

El tablero lista a los empleados con turno ese día (asignación vigente y turno que trabaja ese día de
la semana) y dice en qué va cada uno; los conteos salen de las jornadas de ese día. Quien ese día no
trabaja (festivo o ausencia aprobada, sin tenerlo como laborable) aparece como "Día libre" con su
motivo, nunca como falta; si aun así registró su jornada, manda la jornada.
"""

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.core.clock import as_utc, business_today
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models import AttendanceReviewStatus, BoardState, Employee, User, WorkSession, WorkSessionStatus
from app.repositories.aggregates import LOG_COUNT_CAP, get_scoped
from app.repositories.attendance_repository import AttendanceRepository
from app.repositories.department_repository import DepartmentRepository
from app.repositories.shift_repository import ShiftRepository
from app.schemas.attendance import (
    AttendanceBoard,
    AttendanceEventRead,
    AttendanceHistory,
    AttendanceReviewDecision,
    BoardRow,
    CompanySessionDetail,
    CompanySessionList,
    CompanySessionRead,
    WorkSessionRead,
)
from app.schemas.common import PageParams
from app.services import attendance_service
from app.services.attendance_service import AttendanceService
from app.services.calendar_service import day_off_read
from app.services.shift_rules import Occurrence, occurrence_of
from app.services.shift_service import employee_ref

#: Días máximos de un rango del historial (ambos incluidos), como en Reportes.
HISTORY_MAX_DAYS = 366
#: Lo que la empresa decide de un registro "en revisión".
DECISIONS = (AttendanceReviewStatus.CONFIRMED, AttendanceReviewStatus.REJECTED)


def board_state(
    session: WorkSession | None, on_break: bool, occurrence: Occurrence, now: datetime, *, day_off: bool = False
) -> BoardState:
    """En qué va un empleado (catalog.board_states: nombre y color para la pantalla)."""
    if session is None:
        if day_off:
            return BoardState.DAY_OFF
        if now < occurrence.start:
            return BoardState.SCHEDULED
        return BoardState.ABSENT if now > occurrence.deadline else BoardState.MISSING
    if session.status == WorkSessionStatus.CLOSED:
        return BoardState.DONE
    if session.status == WorkSessionStatus.MISSED_CHECKOUT or as_utc(session.check_out_deadline) < now:
        return BoardState.MISSED_CHECKOUT
    return BoardState.ON_BREAK if on_break else BoardState.WORKING


class AttendanceOverview:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = AttendanceRepository(db, company_id)
        self.shifts = ShiftRepository(db, company_id)
        self.sessions = AttendanceService(db, company_id)

    def board(self, work_date: date | None, *, search: str | None, page: PageParams) -> AttendanceBoard:
        day = work_date or business_today()
        now = attendance_service.now_utc()  # la misma hora que los registros
        rows, total = self.shifts.assigned_on(day, search=search, offset=page.offset, limit=page.size)
        shifts = self.shifts.shifts_by_ids(a.shift_id for a, _ in rows)
        sessions = self.repo.sessions_on((e.id for _, e in rows), day)
        found = list(sessions.values())
        reads = {s.id: r for s, r in zip(found, self.sessions.reads(found), strict=True)}
        departments = DepartmentRepository(self.db, self.company_id).names(
            e.department_id for _, e in rows if e.department_id
        )
        calendar = self.sessions.calendar.days_off((e.id for _, e in rows), day, day)
        items = []
        for assignment, employee in rows:
            occurrence = occurrence_of(shifts[assignment.shift_id], day, self.sessions.zone)
            session = sessions.get(employee.id)
            read = reads[session.id] if session else None
            on_break = bool(read and any(b.ended_at is None for b in read.breaks))
            day_off = None if session else calendar.on(employee.id, day)
            items.append(
                BoardRow(
                    employee=employee_ref(employee),
                    department=departments.get(employee.department_id or 0),
                    shift_name=session.shift_name if session else shifts[assignment.shift_id].name,
                    scheduled_start=session.scheduled_start if session else occurrence.start,
                    scheduled_end=session.scheduled_end if session else occurrence.end,
                    state=board_state(session, on_break, occurrence, now, day_off=day_off is not None),
                    session=read,
                    day_off=day_off_read(day_off, day),
                )
            )
        counts = self._counts(day, holiday=bool(calendar.holidays))
        return AttendanceBoard.of(items, total, page, work_date=day, **counts)

    def _counts(self, day: date, *, holiday: bool) -> dict[str, int]:
        """Conteos de todo el día (no solo de la página): las jornadas por estado (un GROUP BY, sin
        cargar las jornadas) y quienes tienen el día libre (una consulta)."""
        counts = self.repo.day_counts(day)
        open_sessions, on_break = counts.get(WorkSessionStatus.OPEN, (0, 0))
        return {
            "working": open_sessions - on_break,
            "on_break": on_break,
            "done": counts.get(WorkSessionStatus.CLOSED, (0, 0))[0],
            "missed_checkout": counts.get(WorkSessionStatus.MISSED_CHECKOUT, (0, 0))[0],
            "day_off": self.shifts.assigned_off_on(day, holiday=holiday),
        }

    def history(
        self,
        *,
        employee_id: int | None,
        start: date | None,
        end: date | None,
        status: str | None,
        page: PageParams,
        in_review: bool = False,
    ) -> CompanySessionList:
        if start and end and (end < start or (end - start).days + 1 > HISTORY_MAX_DAYS):
            raise UnprocessableError(code="ATTENDANCE_INVALID_PERIOD", params={"count": HISTORY_MAX_DAYS}, field="end")
        items, total = self.repo.history(
            employee_id=employee_id,
            start=start,
            end=end,
            status=status,
            offset=page.offset,
            limit=page.size,
            in_review=in_review,
        )
        return CompanySessionList.of(self._with_employees(items), total, page)

    def _with_employees(self, sessions: list[WorkSession]) -> list[CompanySessionRead]:
        employees = self.shifts.employees_by_ids(s.employee_id for s in sessions)
        return [
            CompanySessionRead(**read.model_dump(), employee=employee_ref(employees[s.employee_id]))
            for s, read in zip(sessions, self.sessions.reads(sessions), strict=True)
        ]

    def detail(self, session_id: int) -> CompanySessionDetail:
        session = self.repo.session(session_id)
        if session is None:
            raise NotFoundError(code="WORK_SESSION_NOT_FOUND")
        read = self._with_employees([session])[0]
        events = self.repo.events_of(session.id)
        names = self.repo.site_names(event.site_id for event, _, _ in events)
        return CompanySessionDetail(
            **read.model_dump(),
            events=[
                AttendanceEventRead(
                    action=event.action,
                    mode=event.mode,
                    site=names.get(event.site_id or 0),
                    occurred_at=event.occurred_at,
                    latitude=event.latitude,
                    longitude=event.longitude,
                    accuracy_m=event.accuracy_m,
                    distance_m=event.distance_m,
                    confidence=score,
                    operator=operator,
                    note=event.note,
                    under_review=event.under_review,
                )
                for event, score, operator in events
            ],
        )

    def mine(self, employee: Employee, page: PageParams) -> AttendanceHistory:
        items, total = self.repo.history(
            employee_id=employee.id, start=None, end=None, status=None, offset=page.offset, limit=page.size
        )
        reads: list[WorkSessionRead] = self.sessions.reads(items, reasons=False)
        return AttendanceHistory.of(reads, total, page)


class AttendanceReview:
    """La empresa confirma o rechaza un registro "en revisión" (decisión D3/D10 del dueño del producto): el registro
    ya está guardado (nadie se quedó sin checar); rechazarlo no lo borra, queda marcado con quién, cuándo y por qué
    (la empresa lo corrige con el registro manual si hace falta)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.overview = AttendanceOverview(db, company_id)

    def pending(self) -> int:
        return self.overview.repo.pending_reviews(LOG_COUNT_CAP)

    def decide(self, session_id: int, data: AttendanceReviewDecision, user: User) -> CompanySessionDetail:
        if data.decision not in DECISIONS:
            raise UnprocessableError(code="INVALID_REVIEW_DECISION", field="decision")
        if data.decision == AttendanceReviewStatus.REJECTED and not data.note:
            raise UnprocessableError(code="REVIEW_NOTE_REQUIRED", field="note")
        session = get_scoped(self.db, WorkSession, session_id, self.company_id, lock=True)
        if session is None:
            raise NotFoundError(code="WORK_SESSION_NOT_FOUND")
        if session.review_status != AttendanceReviewStatus.PENDING:
            raise ConflictError(code="ATTENDANCE_REVIEW_NOT_PENDING")
        session.review_status = data.decision
        session.reviewed_by_id = user.id
        session.reviewed_at = datetime.now(UTC)
        session.review_note = data.note
        self.db.commit()
        return self.overview.detail(session.id)
