"""Consultas de sitios de trabajo, turnos, asignaciones y solicitudes de cambio de UNA empresa.

Un id de otra empresa se comporta como inexistente (404). Los listados cargan lo relacionado por
lotes (una consulta por página, nunca una por elemento). Con borrado lógico (sitios, turnos y asignaciones): toda
consulta ve solo lo vigente, salvo la papelera, eliminar/restaurar (`include_deleted`) y las búsquedas por id que
resuelven referencias del historial (`shifts_by_ids`, `sites_of_shifts`, `employees_by_ids`).
"""

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session, lazyload

from app.core.soft_delete import with_deleted
from app.models import (
    AttendanceEvent,
    Employee,
    EmployeeAbsence,
    EmployeeWorkday,
    Shift,
    ShiftAssignment,
    ShiftChangeRequest,
    ShiftRequestStatus,
    ShiftSite,
    WorkSession,
    WorkSite,
)
from app.repositories.aggregates import affected_rows, get_scoped, group_counts, insert_many, paginate, trash_page
from app.repositories.calendar_repository import absence_touches
from app.repositories.search import contains_text, search_term


def _search(column: Any, search: str | None) -> ColumnElement[bool] | None:
    term = search_term(search)
    return contains_text(func.lower(column), term) if term else None


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
        self, model: type[T], record_id: int, *, lock: bool = False, include_deleted: bool = False
    ) -> T | None:
        return get_scoped(self.db, model, record_id, self.company_id, lock=lock, include_deleted=include_deleted)

    def add[T: (WorkSite, Shift, ShiftAssignment, ShiftChangeRequest)](self, record: T) -> T:
        record.company_id = self.company_id
        self.db.add(record)
        self.db.flush()
        return record

    # ---------- Sitios de trabajo ----------

    def site(self, site_id: int, *, include_deleted: bool = False) -> WorkSite | None:
        return self._get(WorkSite, site_id, include_deleted=include_deleted)

    def sites(
        self, *, search: str | None, active: bool | None, offset: int, limit: int, deleted: bool = False
    ) -> tuple[list[WorkSite], int]:
        """Los vigentes por nombre o, con `deleted`, la papelera (el eliminado más reciente primero)."""
        stmt = select(WorkSite).where(WorkSite.company_id == self.company_id)
        condition = _search(WorkSite.name, search)
        if condition is not None:
            stmt = stmt.where(condition)
        if active is not None:
            stmt = stmt.where(WorkSite.active.is_(active))
        if deleted:
            return trash_page(self.db, stmt, WorkSite, offset=offset, limit=limit)
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

    def shifts_using_site(self, site_id: int, *, limit: int) -> list[str]:
        """Nombres de los turnos que incluyen el sitio (orden alfabético, hasta `limit`): por qué no se
        puede borrar. Índice por sitio de `shift_sites`; la empresa va en ambas tablas."""
        stmt = (
            select(Shift.name)
            .join(ShiftSite, ShiftSite.shift_id == Shift.id)
            .where(
                ShiftSite.company_id == self.company_id,
                ShiftSite.site_id == site_id,
                Shift.company_id == self.company_id,
            )
            .order_by(func.lower(Shift.name), Shift.id)
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def site_has_records(self, site_id: int) -> bool:
        """¿Ya se checó en el sitio? (toda jornada con sitio tiene su registro con ese sitio en la bitácora).

        `EXISTS` con la empresa (§3.1.2) sobre el índice parcial `(company_id, site_id)` (index-only, también con
        la seguridad por fila, que pide `company_id`): se detiene en el primer registro y un sitio sin registros
        —el que sí se puede borrar— cuesta una búsqueda en el índice de cada mes. Medido con la base de volumen:
        0.1 ms en ambos casos, contra 8-29 ms de contar los 148 mil registros de un sitio en uso. (Antes de ese
        índice, `LIMIT 1` recorría TODA la bitácora cuando el sitio no tenía ninguno.) Las FK `RESTRICT` de la
        bitácora y las jornadas no bastan: SQLite no las crea entre esquemas."""
        found = exists().where(AttendanceEvent.company_id == self.company_id, AttendanceEvent.site_id == site_id)
        return bool(self.db.scalar(select(found)))

    def employees_per_site(self, site_ids: Iterable[int], day: date) -> dict[int, int]:
        """Empleados con una asignación vigente ese día cuyo turno incluye cada sitio (a lo más una
        asignación rige cada día: no se enciman)."""
        ids = list(site_ids)
        if not ids:
            return {}
        return group_counts(
            self.db,
            select(ShiftSite.site_id, func.count())
            .join(ShiftAssignment, ShiftAssignment.shift_id == ShiftSite.shift_id)
            .where(
                ShiftSite.company_id == self.company_id,
                ShiftSite.site_id.in_(ids),
                # La empresa también del lado de las asignaciones: sin ella PostgreSQL recorría las de
                # TODAS las empresas (no deduce la igualdad a través del JOIN).
                ShiftAssignment.company_id == self.company_id,
                _valid_on(day),
            )
            .group_by(ShiftSite.site_id),
        )

    # ---------- Turnos ----------

    def shift(self, shift_id: int, *, include_deleted: bool = False) -> Shift | None:
        return self._get(Shift, shift_id, include_deleted=include_deleted)

    def shifts(
        self, *, search: str | None, active: bool | None, offset: int, limit: int, deleted: bool = False
    ) -> tuple[list[Shift], int]:
        """Los vigentes por nombre o, con `deleted`, la papelera (el eliminado más reciente primero)."""
        stmt = select(Shift).where(Shift.company_id == self.company_id)
        condition = _search(Shift.name, search)
        if condition is not None:
            stmt = stmt.where(condition)
        if active is not None:
            stmt = stmt.where(Shift.active.is_(active))
        if deleted:
            return trash_page(self.db, stmt, Shift, offset=offset, limit=limit)
        return paginate(self.db, stmt, (func.lower(Shift.name), Shift.id), offset=offset, limit=limit)

    def shifts_by_ids(self, shift_ids: Iterable[int]) -> dict[int, Shift]:
        """Turnos por id (asignaciones, solicitudes, jornadas): referencias del historial, también en «Eliminados»."""
        ids = list(set(shift_ids))
        if not ids:
            return {}
        stmt = select(Shift).where(Shift.company_id == self.company_id, Shift.id.in_(ids))
        return {shift.id: shift for shift in self.db.scalars(with_deleted(stmt))}

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

    def sites_of_shifts(self, shift_ids: Iterable[int]) -> dict[int, list[WorkSite]]:
        """Sitios de cada turno (orden alfabético): una consulta para toda la página (llave primaria
        `shift_id, site_id` de `shift_sites`). Referencias: también los que están en «Eliminados» (un turno vigente
        nunca tiene uno, `SITE_IN_USE`; uno eliminado o una asignación del historial los muestran con su marca)."""
        ids = list(set(shift_ids))
        if not ids:
            return {}
        stmt = (
            select(ShiftSite.shift_id, WorkSite)
            .join(WorkSite, WorkSite.id == ShiftSite.site_id)
            .where(
                ShiftSite.company_id == self.company_id,
                ShiftSite.shift_id.in_(ids),
                WorkSite.company_id == self.company_id,
            )
            .order_by(func.lower(WorkSite.name), WorkSite.id)
        )
        rows = self.db.execute(with_deleted(stmt)).all()
        found: dict[int, list[WorkSite]] = {shift_id: [] for shift_id in ids}
        for shift_id, site in rows:
            found[shift_id].append(site)
        return found

    def deleted_sites_of(self, shift_id: int) -> list[str]:
        """Nombres de los sitios del turno que están en «Eliminados» (restaurar el turno los necesita vigentes)."""
        stmt = (
            select(WorkSite.name)
            .join(ShiftSite, ShiftSite.site_id == WorkSite.id)
            .where(
                ShiftSite.company_id == self.company_id,
                ShiftSite.shift_id == shift_id,
                WorkSite.company_id == self.company_id,
                WorkSite.deleted_at.is_not(None),
            )
            .order_by(func.lower(WorkSite.name), WorkSite.id)
        )
        return list(self.db.scalars(with_deleted(stmt)))

    def shift_site_ids(self, shift_id: int) -> set[int]:
        """Los sitios que el turno tiene hoy (llave primaria de `shift_sites`)."""
        stmt = select(ShiftSite.site_id).where(ShiftSite.company_id == self.company_id, ShiftSite.shift_id == shift_id)
        return set(self.db.scalars(stmt))

    def set_shift_sites(self, shift_id: int, current: set[int], wanted: set[int]) -> None:
        """Los sitios del turno quedan exactamente `wanted`: una sentencia borra los que salen y una
        inserción agrega los que entran (los que siguen no se tocan)."""
        removed = current - wanted
        if removed:
            affected_rows(
                self.db,
                delete(ShiftSite).where(
                    ShiftSite.company_id == self.company_id,
                    ShiftSite.shift_id == shift_id,
                    ShiftSite.site_id.in_(removed),
                ),
            )
        self.db.add_all(
            ShiftSite(shift_id=shift_id, site_id=site_id, company_id=self.company_id)
            for site_id in sorted(wanted - current)
        )
        self.db.flush()

    def shift_of(self, assignment_id: int) -> Shift | None:
        """El turno de una asignación (dónde y cuándo checa una jornada abierta) en una consulta."""
        stmt = (
            select(Shift)
            .join(ShiftAssignment, ShiftAssignment.shift_id == Shift.id)
            .where(
                ShiftAssignment.company_id == self.company_id,
                ShiftAssignment.id == assignment_id,
                Shift.company_id == self.company_id,
            )
        )
        return self.db.scalar(stmt)

    # ---------- Asignaciones ----------

    def assignment(self, assignment_id: int, *, include_deleted: bool = False) -> ShiftAssignment | None:
        return self._get(ShiftAssignment, assignment_id, include_deleted=include_deleted)

    def assignments_of(
        self, employee_id: int, *, offset: int, limit: int, deleted: bool = False
    ) -> tuple[list[ShiftAssignment], int]:
        """Las asignaciones vigentes del empleado (la más reciente primero) o, con `deleted`, sus cambios cancelados
        (el más reciente primero; el súper-índice del empleado lleva `deleted_at` en su INCLUDE)."""
        stmt = select(ShiftAssignment).where(
            ShiftAssignment.company_id == self.company_id, ShiftAssignment.employee_id == employee_id
        )
        if deleted:
            return trash_page(self.db, stmt, ShiftAssignment, offset=offset, limit=limit)
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
        term = search_term(search)
        if term:
            full_name = func.lower(Employee.first_name.concat(" ").concat(Employee.last_name))
            stmt = stmt.where(
                or_(contains_text(full_name, term), contains_text(func.lower(Employee.employee_number), term))
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

    def cancel_pending_requests(
        self, *, actor_id: int, now: datetime, employee_id: int | None = None, shift_id: int | None = None
    ) -> None:
        """Las solicitudes PENDIENTES de un empleado o de un turno que se eliminan quedan canceladas (una sentencia;
        el empleado ve el cambio en su lista). Antes se borraban en cascada con el turno o el empleado."""
        stmt = update(ShiftChangeRequest).where(
            ShiftChangeRequest.company_id == self.company_id, ShiftChangeRequest.status == ShiftRequestStatus.PENDING
        )
        if employee_id is not None:
            stmt = stmt.where(ShiftChangeRequest.employee_id == employee_id)
        if shift_id is not None:
            stmt = stmt.where(ShiftChangeRequest.shift_id == shift_id)
        values = {"status": ShiftRequestStatus.CANCELLED, "reviewed_by_id": actor_id, "reviewed_at": now}
        affected_rows(self.db, stmt.values(**values))

    def has_pending_request(self, employee_id: int) -> bool:
        stmt = select(ShiftChangeRequest.id).where(
            ShiftChangeRequest.company_id == self.company_id,
            ShiftChangeRequest.employee_id == employee_id,
            ShiftChangeRequest.status == ShiftRequestStatus.PENDING,
        )
        return self.db.scalar(stmt.limit(1)) is not None

    def employees_by_ids(self, employee_ids: Iterable[int]) -> dict[int, Employee]:
        """Empleados por id (solicitudes, jornadas): referencias del historial, también en «Eliminados»."""
        ids = list(set(employee_ids))
        if not ids:
            return {}
        stmt = select(Employee).where(Employee.company_id == self.company_id, Employee.id.in_(ids))
        return {employee.id: employee for employee in self.db.scalars(with_deleted(stmt))}
