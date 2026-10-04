"""Consultas de sitios de trabajo, turnos, asignaciones y solicitudes de cambio de UNA empresa.

Un id de otra empresa se comporta como inexistente (404). Los listados cargan lo relacionado por
lotes (una consulta por página, nunca una por elemento).
"""

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session, lazyload

from app.models import (
    Employee,
    EmployeeAbsence,
    EmployeeWorkday,
    Shift,
    ShiftAssignment,
    ShiftAssignmentSite,
    ShiftChangeRequest,
    ShiftRequestStatus,
    WorkSession,
    WorkSite,
)
from app.repositories.aggregates import affected_rows, group_counts, insert_many, paginate
from app.repositories.calendar_repository import absence_touches


def _search(column: Any, search: str | None) -> ColumnElement[bool] | None:
    term = " ".join((search or "").split()).lower()
    return func.lower(column).contains(term, autoescape=True) if term else None


def _valid_on(day: date) -> ColumnElement[bool]:
    """La asignación rige ese día."""
    return and_(
        ShiftAssignment.valid_from <= day,
        or_(ShiftAssignment.valid_to.is_(None), ShiftAssignment.valid_to >= day),
    )


def _rules_on(assignment: ShiftAssignment, day: date) -> bool:
    """Lo mismo que `_valid_on`, con la asignación ya leída."""
    return assignment.valid_from <= day and (assignment.valid_to is None or assignment.valid_to >= day)


class ShiftRepository:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def _get[T: (WorkSite, Shift, ShiftAssignment, ShiftChangeRequest)](
        self, model: type[T], record_id: int, *, lock: bool = False
    ) -> T | None:
        record = self.db.get(model, record_id, with_for_update=lock or None, populate_existing=lock)
        return record if record is not None and record.company_id == self.company_id else None

    def add[T: (WorkSite, Shift, ShiftAssignment, ShiftChangeRequest)](self, record: T) -> T:
        record.company_id = self.company_id
        self.db.add(record)
        self.db.flush()
        return record

    # ---------- Sitios de trabajo ----------

    def site(self, site_id: int) -> WorkSite | None:
        return self._get(WorkSite, site_id)

    def sites(self, *, search: str | None, active: bool | None, offset: int, limit: int) -> tuple[list[WorkSite], int]:
        stmt = select(WorkSite).where(WorkSite.company_id == self.company_id)
        condition = _search(WorkSite.name, search)
        if condition is not None:
            stmt = stmt.where(condition)
        if active is not None:
            stmt = stmt.where(WorkSite.active.is_(active))
        return paginate(self.db, stmt, (func.lower(WorkSite.name), WorkSite.id), offset=offset, limit=limit)

    def sites_by_ids(self, site_ids: Iterable[int]) -> list[WorkSite]:
        ids = list(set(site_ids))
        if not ids:
            return []
        stmt = select(WorkSite).where(WorkSite.company_id == self.company_id, WorkSite.id.in_(ids))
        return list(self.db.scalars(stmt.order_by(func.lower(WorkSite.name), WorkSite.id)))

    def site_name_exists(self, name: str, exclude_id: int | None = None) -> bool:
        stmt = select(WorkSite.id).where(
            WorkSite.company_id == self.company_id, func.lower(WorkSite.name) == name.lower()
        )
        if exclude_id is not None:
            stmt = stmt.where(WorkSite.id != exclude_id)
        return self.db.scalar(stmt.limit(1)) is not None

    def site_in_use(self, site_id: int) -> bool:
        stmt = select(ShiftAssignmentSite.site_id).where(
            ShiftAssignmentSite.company_id == self.company_id, ShiftAssignmentSite.site_id == site_id
        )
        return self.db.scalar(stmt.limit(1)) is not None

    def delete_site(self, site: WorkSite) -> None:
        affected_rows(self.db, delete(WorkSite).where(WorkSite.company_id == self.company_id, WorkSite.id == site.id))
        self.db.expunge(site)

    def employees_per_site(self, site_ids: Iterable[int], day: date) -> dict[int, int]:
        """Empleados con una asignación vigente ese día que incluye cada sitio."""
        ids = list(site_ids)
        if not ids:
            return {}
        return group_counts(
            self.db,
            select(ShiftAssignmentSite.site_id, func.count())
            .join(ShiftAssignment, ShiftAssignment.id == ShiftAssignmentSite.assignment_id)
            .where(
                ShiftAssignmentSite.company_id == self.company_id,
                ShiftAssignmentSite.site_id.in_(ids),
                # La empresa también del lado de las asignaciones: sin ella PostgreSQL recorría las de
                # TODAS las empresas (no deduce la igualdad a través del JOIN).
                ShiftAssignment.company_id == self.company_id,
                _valid_on(day),
            )
            .group_by(ShiftAssignmentSite.site_id),
        )

    # ---------- Turnos ----------

    def shift(self, shift_id: int) -> Shift | None:
        return self._get(Shift, shift_id)

    def shifts(self, *, search: str | None, active: bool | None, offset: int, limit: int) -> tuple[list[Shift], int]:
        stmt = select(Shift).where(Shift.company_id == self.company_id)
        condition = _search(Shift.name, search)
        if condition is not None:
            stmt = stmt.where(condition)
        if active is not None:
            stmt = stmt.where(Shift.active.is_(active))
        return paginate(self.db, stmt, (func.lower(Shift.name), Shift.id), offset=offset, limit=limit)

    def shifts_by_ids(self, shift_ids: Iterable[int]) -> dict[int, Shift]:
        ids = list(set(shift_ids))
        if not ids:
            return {}
        stmt = select(Shift).where(Shift.company_id == self.company_id, Shift.id.in_(ids))
        return {shift.id: shift for shift in self.db.scalars(stmt)}

    def shift_name_exists(self, name: str, exclude_id: int | None = None) -> bool:
        stmt = select(Shift.id).where(Shift.company_id == self.company_id, func.lower(Shift.name) == name.lower())
        if exclude_id is not None:
            stmt = stmt.where(Shift.id != exclude_id)
        return self.db.scalar(stmt.limit(1)) is not None

    def shift_in_use(self, shift_id: int) -> bool:
        assigned = select(ShiftAssignment.id).where(
            ShiftAssignment.company_id == self.company_id, ShiftAssignment.shift_id == shift_id
        )
        return self.db.scalar(assigned.limit(1)) is not None

    def delete_shift(self, shift: Shift) -> None:
        """Sus solicitudes de cambio se van con él (ON DELETE CASCADE); con asignaciones no se borra."""
        affected_rows(self.db, delete(Shift).where(Shift.company_id == self.company_id, Shift.id == shift.id))
        self.db.expunge(shift)

    def employees_per_shift(self, shift_ids: Iterable[int], day: date) -> dict[int, int]:
        ids = list(shift_ids)
        if not ids:
            return {}
        return group_counts(
            self.db,
            select(ShiftAssignment.shift_id, func.count())
            .where(ShiftAssignment.company_id == self.company_id, ShiftAssignment.shift_id.in_(ids), _valid_on(day))
            .group_by(ShiftAssignment.shift_id),
        )

    # ---------- Asignaciones ----------

    def assignment(self, assignment_id: int) -> ShiftAssignment | None:
        return self._get(ShiftAssignment, assignment_id)

    def assignments_of(self, employee_id: int, *, offset: int, limit: int) -> tuple[list[ShiftAssignment], int]:
        stmt = select(ShiftAssignment).where(
            ShiftAssignment.company_id == self.company_id, ShiftAssignment.employee_id == employee_id
        )
        order = (ShiftAssignment.valid_from.desc(), ShiftAssignment.id.desc())
        return paginate(self.db, stmt, order, offset=offset, limit=limit)

    def assignment_on(self, employee_id: int, day: date) -> ShiftAssignment | None:
        """La asignación que rige ese día (a lo más una: no se encimen)."""
        stmt = select(ShiftAssignment).where(
            ShiftAssignment.company_id == self.company_id, ShiftAssignment.employee_id == employee_id, _valid_on(day)
        )
        return self.db.scalar(stmt.limit(1))

    def assignments_on(self, employee_id: int, days: list[date]) -> dict[date, ShiftAssignment]:
        """La asignación de cada día (turnos nocturnos que vienen de ayer, la siguiente jornada): una
        consulta para todo el rango (a lo más una rige cada día: no se enciman)."""
        first, last = min(days), max(days)
        stmt = select(ShiftAssignment).where(
            ShiftAssignment.company_id == self.company_id,
            ShiftAssignment.employee_id == employee_id,
            ShiftAssignment.valid_from <= last,
            or_(ShiftAssignment.valid_to.is_(None), ShiftAssignment.valid_to >= first),
        )
        assignments = list(self.db.scalars(stmt))
        found: dict[date, ShiftAssignment] = {}
        for day in days:
            assignment = next((a for a in assignments if _rules_on(a, day)), None)
            if assignment is not None:
                found[day] = assignment
        return found

    def current_assignments(self, employee_ids: Iterable[int], day: date) -> dict[int, ShiftAssignment]:
        """La asignación vigente ese día de cada empleado: una consulta para toda la página."""
        ids = list(employee_ids)
        if not ids:
            return {}
        stmt = select(ShiftAssignment).where(
            ShiftAssignment.company_id == self.company_id, ShiftAssignment.employee_id.in_(ids), _valid_on(day)
        )
        return {assignment.employee_id: assignment for assignment in self.db.scalars(stmt)}

    def employees_with_assignments(self, employee_ids: Iterable[int]) -> set[int]:
        """Cuáles de esos empleados ya tuvieron alguna vez un turno (su cambio requiere anticipación)."""
        stmt = select(ShiftAssignment.employee_id).where(
            ShiftAssignment.company_id == self.company_id, ShiftAssignment.employee_id.in_(list(employee_ids))
        )
        return set(self.db.scalars(stmt.distinct()))

    def assignments_from(self, employee_ids: Iterable[int], day: date) -> dict[int, list[ShiftAssignment]]:
        """Las asignaciones de cada empleado que rigen ese día o después (la vigente y los cambios ya
        programados), de la más antigua a la más nueva: una consulta para todos."""
        ids = list(employee_ids)
        found: dict[int, list[ShiftAssignment]] = {employee_id: [] for employee_id in ids}
        stmt = select(ShiftAssignment).where(
            ShiftAssignment.company_id == self.company_id,
            ShiftAssignment.employee_id.in_(ids),
            or_(ShiftAssignment.valid_to.is_(None), ShiftAssignment.valid_to >= day),
        )
        for assignment in self.db.scalars(stmt.order_by(ShiftAssignment.valid_from, ShiftAssignment.id)):
            found[assignment.employee_id].append(assignment)
        return found

    def close_before(self, employee_ids: Iterable[int], day: date) -> None:
        """La asignación que sigue vigente al iniciar otra termina el día anterior: una sentencia para
        todos los empleados que cambian de turno ese día (al menos uno)."""
        ids = list(employee_ids)
        affected_rows(
            self.db,
            update(ShiftAssignment)
            .where(
                ShiftAssignment.company_id == self.company_id,
                ShiftAssignment.employee_id.in_(ids),
                ShiftAssignment.valid_from < day,
                or_(ShiftAssignment.valid_to.is_(None), ShiftAssignment.valid_to >= day),
            )
            .values(valid_to=day - timedelta(days=1)),
        )

    def add_assignments(self, assignments: list[ShiftAssignment]) -> list[ShiftAssignment]:
        """Varias asignaciones en una sola inserción (asignar a varios empleados a la vez); las guardadas
        vuelven en cualquier orden."""
        for assignment in assignments:
            assignment.company_id = self.company_id
        return insert_many(self.db, assignments)

    def delete_assignment(self, assignment: ShiftAssignment) -> None:
        affected_rows(
            self.db,
            delete(ShiftAssignment).where(
                ShiftAssignment.company_id == self.company_id, ShiftAssignment.id == assignment.id
            ),
        )
        self.db.expunge(assignment)

    def reopen_previous(self, employee_id: int, valid_to: date) -> None:
        """Al cancelar un cambio programado, la asignación que terminaba un día antes vuelve a no tener fin."""
        affected_rows(
            self.db,
            update(ShiftAssignment)
            .where(
                ShiftAssignment.company_id == self.company_id,
                ShiftAssignment.employee_id == employee_id,
                ShiftAssignment.valid_to == valid_to,
            )
            .values(valid_to=None),
        )

    def set_sites(self, assignments: Iterable[ShiftAssignment], site_ids: Iterable[int]) -> None:
        """Los mismos sitios para cada asignación (una inserción para todas)."""
        unique = set(site_ids)
        self.db.add_all(
            ShiftAssignmentSite(assignment_id=assignment.id, site_id=site_id, company_id=self.company_id)
            for assignment in assignments
            for site_id in unique
        )
        self.db.flush()

    def sites_of(self, assignment_ids: Iterable[int]) -> dict[int, list[WorkSite]]:
        """Sitios de cada asignación (orden alfabético): una consulta para toda la página."""
        ids = list(assignment_ids)
        if not ids:
            return {}
        rows = self.db.execute(
            select(ShiftAssignmentSite.assignment_id, WorkSite)
            .join(WorkSite, WorkSite.id == ShiftAssignmentSite.site_id)
            .where(ShiftAssignmentSite.company_id == self.company_id, ShiftAssignmentSite.assignment_id.in_(ids))
            .order_by(func.lower(WorkSite.name), WorkSite.id)
        ).all()
        found: dict[int, list[WorkSite]] = {assignment_id: [] for assignment_id in ids}
        for assignment_id, site in rows:
            found[assignment_id].append(site)
        return found

    def _assigned(self, day: date) -> Select[ShiftAssignment, Employee]:
        """Empleados activos con una asignación vigente ese día cuyo turno trabaja ese día de la semana.

        La empresa va en CADA tabla (no solo en las asignaciones): PostgreSQL no deduce la igualdad a
        través del JOIN y, sin ella, recorría los empleados y turnos de todas las empresas. Las
        asignaciones de la empresa salen del índice (company_id, employee_id, valid_from) INCLUDE
        (valid_to, shift_id) sin leer la tabla."""
        return (
            select(ShiftAssignment, Employee)
            .join(Employee, Employee.id == ShiftAssignment.employee_id)
            .join(Shift, Shift.id == ShiftAssignment.shift_id)
            .where(
                ShiftAssignment.company_id == self.company_id,
                Employee.company_id == self.company_id,
                Shift.company_id == self.company_id,
                Employee.active.is_(True),
                Shift.active.is_(True),
                Shift.weekdays.op("&")(1 << day.weekday()) != 0,  # el turno trabaja ese día
                _valid_on(day),
            )
        )

    def assigned_off_on(self, day: date, *, holiday: bool) -> int:
        """Cuántos de los que tienen turno ese día no lo trabajan: festivo (`holiday`) o ausencia
        aprobada que lo cubre, salvo quien lo tiene como laborable o registró su jornada (en el tablero
        manda la jornada). El conteo "Día libre" del tablero en una consulta (la misma regla que
        `calendar_rules.day_off_on`).

        Cada subconsulta lleva la empresa y el día: "registró su jornada" se resuelve con las jornadas
        de ese día de la empresa (índice company_id + work_date INCLUDE employee_id) en lugar de leer
        todo el historial de cada empleado, y la ausencia se acota con `absence_touches`."""
        stmt = self._assigned(day).where(
            ~exists().where(
                EmployeeWorkday.company_id == self.company_id,
                EmployeeWorkday.employee_id == Employee.id,
                EmployeeWorkday.work_date == day,
            ),
            ~exists().where(
                WorkSession.company_id == self.company_id,
                WorkSession.work_date == day,
                WorkSession.employee_id == Employee.id,
            ),
        )
        if not holiday:
            stmt = stmt.where(
                exists().where(
                    EmployeeAbsence.company_id == self.company_id,
                    EmployeeAbsence.employee_id == Employee.id,
                    EmployeeAbsence.status == ShiftRequestStatus.APPROVED,
                    *absence_touches(day, day),
                )
            )
        return int(self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)

    def assigned_on(
        self, day: date, *, search: str | None, offset: int, limit: int
    ) -> tuple[list[tuple[ShiftAssignment, Employee]], int]:
        """Empleados activos con una asignación vigente ese día (tablero), por nombre."""
        stmt = self._assigned(day)
        term = " ".join((search or "").split()).lower()
        if term:
            full_name = func.lower(Employee.first_name.concat(" ").concat(Employee.last_name))
            stmt = stmt.where(
                or_(
                    full_name.contains(term, autoescape=True),
                    func.lower(Employee.employee_number).contains(term, autoescape=True),
                )
            )
        total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        # Sin los JOIN de carga de la cuenta y la empresa del empleado (el tablero no los usa): con ellos
        # PostgreSQL unía TODAS las filas del día con `users` antes de ordenar y cortar la página.
        page = (
            stmt.options(lazyload(Employee.user), lazyload(Employee.company))
            .order_by(Employee.last_name, Employee.first_name, Employee.id)
            .offset(offset)
            .limit(limit)
        )
        rows = self.db.execute(page).all()
        return [(assignment, employee) for assignment, employee in rows], int(total)

    # ---------- Solicitudes de cambio de turno ----------

    def request(self, request_id: int, *, lock: bool = False) -> ShiftChangeRequest | None:
        return self._get(ShiftChangeRequest, request_id, lock=lock)

    def requests(
        self, *, status: str | None, employee_id: int | None, offset: int, limit: int
    ) -> tuple[list[ShiftChangeRequest], int]:
        stmt = select(ShiftChangeRequest).where(ShiftChangeRequest.company_id == self.company_id)
        if status is not None:
            stmt = stmt.where(ShiftChangeRequest.status == status)
        if employee_id is not None:
            stmt = stmt.where(ShiftChangeRequest.employee_id == employee_id)
        return paginate(self.db, stmt, (ShiftChangeRequest.id.desc(),), offset=offset, limit=limit)

    def pending_requests(self) -> int:
        stmt = select(func.count()).where(
            ShiftChangeRequest.company_id == self.company_id, ShiftChangeRequest.status == ShiftRequestStatus.PENDING
        )
        return int(self.db.scalar(stmt) or 0)

    def has_pending_request(self, employee_id: int) -> bool:
        stmt = select(ShiftChangeRequest.id).where(
            ShiftChangeRequest.company_id == self.company_id,
            ShiftChangeRequest.employee_id == employee_id,
            ShiftChangeRequest.status == ShiftRequestStatus.PENDING,
        )
        return self.db.scalar(stmt.limit(1)) is not None

    def employees_by_ids(self, employee_ids: Iterable[int]) -> dict[int, Employee]:
        ids = list(set(employee_ids))
        if not ids:
            return {}
        stmt = select(Employee).where(Employee.company_id == self.company_id, Employee.id.in_(ids))
        return {employee.id: employee for employee in self.db.scalars(stmt)}
