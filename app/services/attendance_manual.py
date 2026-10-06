"""La empresa registra o corrige la jornada de un empleado (decisión del dueño del producto: SOLO la empresa).

- Registrar: la jornada del turno de un día en que el empleado no checó (entrada, salida opcional y
  sus descansos). Corregir: las horas y los descansos de una jornada ya registrada.
- Sin rostro ni ubicación: lo respalda la empresa con un motivo obligatorio. La jornada guarda quién,
  cuándo y por qué (`edited_by_id`, `edited_at`, `edit_reason`) y la bitácora suma un registro por
  cada hora declarada con la modalidad COMPANY y el motivo; lo que había se conserva como evidencia.
  El historial del empleado lo muestra como "Registrado por la empresa".
- Las mismas reglas del registro en vivo (`attendance_rules`): ventanas del turno, nada en el futuro,
  descansos dentro del horario, a lo más los del turno y sin encimarse. Los retardos, la salida
  anticipada, los descansos y los minutos trabajados se calculan con las mismas funciones
  (`new_session`, `end_break`, `finish`).
- Solo en un día de su turno que no sea libre (si trabajó un festivo o en sus vacaciones, primero se
  marca ese día como laborable en Calendario) y una jornada por turno (si ya existe, se corrige).
- Con el empleado bloqueado (`FOR UPDATE OF employees`), como el registro en vivo: no se cruza con
  una checada del propio empleado.
"""

from datetime import UTC, datetime, time, timedelta
from typing import cast

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models import (
    AttendanceAction,
    AttendanceEvent,
    Employee,
    ShiftAssignment,
    User,
    WorkBreak,
    WorkMode,
    WorkSession,
    WorkSessionStatus,
)
from app.repositories.employee_repository import EmployeeRepository
from app.schemas.attendance import CompanySessionDetail, ManualSessionCreate, ManualSessionUpdate, ManualTimes
from app.services import attendance_service
from app.services.attendance_overview import AttendanceOverview
from app.services.attendance_rules import RecordTimes, record_problem
from app.services.attendance_service import end_break, finish, new_session
from app.services.shift_rules import Occurrence, late_minutes, occurrence_of, resolve_clock, works_on


class ManualAttendance:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.overview = AttendanceOverview(db, company_id)
        self.sessions = self.overview.sessions
        self.repo = self.sessions.repo
        self.shifts = self.sessions.shifts
        self.zone = self.sessions.zone

    def create(self, data: ManualSessionCreate, actor: User) -> CompanySessionDetail:
        """Registra la jornada de un día de su turno en que no checó."""
        employee = EmployeeRepository(self.db, self.company_id).get_by_id(data.employee_id, lock=True)
        if employee is None:
            raise NotFoundError(code="EMPLOYEE_NOT_FOUND")
        assignment = self.shifts.assignment_on(employee.id, data.work_date)
        shift = self.shifts.shifts_by_ids([assignment.shift_id])[assignment.shift_id] if assignment else None
        if assignment is None or shift is None or not shift.active or not works_on(shift, data.work_date):
            raise UnprocessableError(
                code="NO_SHIFT_THAT_DAY", params={"name": employee.full_name, "date": data.work_date}, field="work_date"
            )
        occurrence = occurrence_of(shift, data.work_date, self.zone)
        self._ensure_workday(employee, occurrence)
        if self.repo.session_at(employee.id, occurrence.start.astimezone(UTC)) is not None:
            raise ConflictError(
                code="ATTENDANCE_SESSION_EXISTS",
            )
        now = attendance_service.now_utc()
        times = self._times(data, occurrence, now, shift.breaks_count)
        session = new_session(assignment, shift, occurrence, times.check_in, WorkMode.COMPANY)
        self._ensure_single_open(session, times, now)
        self.repo.add(session)
        self._apply(session, times, now, actor, data.reason, changed=(True, True))
        return self.overview.detail(session.id)

    def correct(self, session_id: int, data: ManualSessionUpdate, actor: User) -> CompanySessionDetail:
        """Corrige las horas y los descansos de una jornada (lo que había queda en la bitácora)."""
        found = self.repo.session(session_id)
        if found is None:
            raise NotFoundError(code="WORK_SESSION_NOT_FOUND")
        EmployeeRepository(self.db, self.company_id).get_by_id(found.employee_id, lock=True)
        self.db.refresh(found)  # lo último, ya con el empleado bloqueado (una checada pudo cambiarla)
        session = found
        # La asignación de una jornada siempre existe (FK con RESTRICT).
        assignment = cast(ShiftAssignment, self.shifts.assignment(session.assignment_id))
        shift = self.shifts.shifts_by_ids([assignment.shift_id])[assignment.shift_id]
        start = as_utc(session.scheduled_start)
        occurrence = Occurrence(
            work_date=session.work_date,
            start=start,
            end=as_utc(session.scheduled_end),
            opens=start - timedelta(minutes=shift.early_check_in_minutes),
            deadline=as_utc(session.check_out_deadline),
        )
        now = attendance_service.now_utc()
        times = self._times(data, occurrence, now, session.breaks_allowed)
        self._ensure_single_open(session, times, now)
        check_in_changed = times.check_in != as_utc(session.check_in_at)
        check_out_changed = times.check_out != as_utc(session.check_out_at)
        if check_in_changed:
            session.check_in_at = times.check_in
            session.check_in_mode = WorkMode.COMPANY
            session.check_in_site_id = None
            session.late_minutes = late_minutes(shift, occurrence, times.check_in)
        self.repo.delete_breaks(session.id)
        self._apply(session, times, now, actor, data.reason, changed=(check_in_changed, check_out_changed))
        return self.overview.detail(session.id)

    # ---------- Internos ----------

    def _ensure_workday(self, employee: Employee, occurrence: Occurrence) -> None:
        """Un día libre no se registra: si sí trabajó, primero se marca como laborable en Calendario."""
        work_date = occurrence.work_date
        day_off = self.sessions.calendar.days_off([employee.id], work_date, work_date).on(employee.id, work_date)
        if day_off is not None:
            raise ConflictError(
                code="DAY_OFF",
                key="DAY_OFF_MANUAL",
                params={"reason": day_off.name, "date": work_date, "name": employee.full_name},
                details={"kind": day_off.kind, "name": day_off.name},
            )

    def _times(self, data: ManualTimes, occurrence: Occurrence, now: datetime, breaks_allowed: int) -> RecordTimes:
        """Las horas declaradas como fecha y hora de esa jornada (UTC), ya validadas."""

        def at(clock: time) -> datetime:
            return resolve_clock(occurrence, clock, self.zone).astimezone(UTC)

        times = RecordTimes(
            check_in=at(data.check_in),
            check_out=at(data.check_out) if data.check_out is not None else None,
            breaks=sorted((at(b.start), at(b.end)) for b in data.breaks),
        )
        problem = record_problem(times, occurrence, now, breaks_allowed=breaks_allowed, zone=self.zone)
        if problem is not None:
            raise problem
        return times

    def _ensure_single_open(self, session: WorkSession, times: RecordTimes, now: datetime) -> None:
        """Si la jornada queda abierta, no puede haber otra abierta del empleado (una vencida se marca
        sin salida, como al checar)."""
        if times.check_out is not None or now > as_utc(session.check_out_deadline):
            return
        other = self.repo.open_session(session.employee_id, lock=True)
        if other is None or other.id == session.id:
            return
        if as_utc(other.check_out_deadline) < now:
            self.sessions.expire(other)
            return
        raise ConflictError(code="ATTENDANCE_SESSION_OPEN", params={"date": other.work_date})

    def _apply(
        self,
        session: WorkSession,
        times: RecordTimes,
        now: datetime,
        actor: User,
        reason: str,
        *,
        changed: tuple[bool, bool],
    ) -> None:
        """Descansos, salida y estado con las funciones del registro en vivo; quién, cuándo y por qué; y
        un registro de la bitácora por cada hora declarada (modalidad COMPANY con el motivo)."""
        session.break_minutes = 0
        breaks = [WorkBreak(session_id=session.id, started_at=start) for start, _ in times.breaks]
        for item, (_, end) in zip(breaks, times.breaks, strict=True):
            end_break(session, item, end)
        self.repo.add_all(breaks)
        if times.check_out is not None:
            same = not changed[1]
            finish(
                session,
                times.check_out,
                WorkMode(session.check_out_mode) if same and session.check_out_mode else WorkMode.COMPANY,
                session.check_out_site_id if same else None,
            )
        else:
            session.check_out_at = session.check_out_mode = session.check_out_site_id = None
            session.early_leave_minutes = 0
            session.worked_minutes = None
            expired = now > as_utc(session.check_out_deadline)
            session.status = WorkSessionStatus.MISSED_CHECKOUT if expired else WorkSessionStatus.OPEN
        session.edited_by_id = actor.id
        session.edited_at = now
        session.edit_reason = reason
        moments = [(AttendanceAction.CHECK_IN, times.check_in)]
        for start, end in times.breaks:
            moments += [(AttendanceAction.BREAK_START, start), (AttendanceAction.BREAK_END, end)]
        if times.check_out is not None:
            moments.append((AttendanceAction.CHECK_OUT, times.check_out))
        self.repo.add_all(
            [
                AttendanceEvent(
                    employee_id=session.employee_id,
                    session_id=session.id,
                    action=action,
                    mode=WorkMode.COMPANY,
                    occurred_at=moment,
                    actor_id=actor.id,
                    note=reason,
                )
                for action, moment in moments
            ]
        )
        self.db.commit()
