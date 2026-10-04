"""Turnos de una empresa y su asignación a los empleados.

Cambiar el turno de un empleado nunca toca lo ya registrado:
- Si ya tiene turno, el nuevo empieza mañana o después (`ASSIGNMENT_NOTICE_DAYS`): la asignación
  vigente termina el día anterior y las jornadas ya registradas conservan el turno con que ocurrieron
  (`WorkSession` guarda su horario). Solo la primera asignación puede empezar hoy.
- Un cambio ya programado se puede cancelar mientras no empiece; la asignación anterior recupera su
  vigencia.
- Las asignaciones no se enciman: un empleado tiene a lo más un turno cada día (el empleado se lee
  con candado de fila para que dos cambios simultáneos no se crucen).

Editar un turno aplica a las jornadas que aún no empiezan; las registradas guardan su horario.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.exceptions import AppError, ConflictError, NotFoundError, UnprocessableError
from app.models import AssignmentState, Employee, Shift, ShiftAssignment, User
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.shift_repository import ShiftRepository
from app.schemas.bulk import BulkOutcome, BulkResult, BulkResultCode
from app.schemas.common import EmployeeRef, PageParams
from app.schemas.shift import (
    AssignmentBulkCreate,
    AssignmentCreate,
    AssignmentList,
    AssignmentRead,
    ShiftCreate,
    ShiftList,
    ShiftRead,
    ShiftRef,
    ShiftUpdate,
)
from app.services.shift_rules import WEEKDAY_NAMES, duration_minutes, mask_of, overnight, weekdays_of
from app.services.site_service import site_ref

#: Un cambio de turno se programa con al menos un día de anticipación.
ASSIGNMENT_NOTICE_DAYS = 1
SHIFT_NAME_TAKEN = "Ya existe un turno con ese nombre"
ASSIGNMENT_IN_PAST = "El turno no puede empezar en una fecha pasada"


def employee_ref(employee: Employee) -> EmployeeRef:
    return EmployeeRef(id=employee.id, full_name=employee.full_name, employee_number=employee.employee_number)


def shift_ref(shift: Shift) -> ShiftRef:
    return ShiftRef(
        id=shift.id,
        name=shift.name,
        start_time=shift.start_time,
        end_time=shift.end_time,
        overnight=overnight(shift),
        weekdays=weekdays_of(shift.weekdays),
    )


def _invalid(message: str, code: str, field: str) -> UnprocessableError:
    return UnprocessableError(message, code=code, field=field)


class ShiftService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = ShiftRepository(db, company_id)

    # ---------- Turnos ----------

    @staticmethod
    def _read(shift: Shift, employees: int) -> ShiftRead:
        return ShiftRead(
            **shift_ref(shift).model_dump(),
            breaks_count=shift.breaks_count,
            break_minutes=shift.break_minutes,
            early_check_in_minutes=shift.early_check_in_minutes,
            late_tolerance_minutes=shift.late_tolerance_minutes,
            early_check_out_minutes=shift.early_check_out_minutes,
            late_check_out_minutes=shift.late_check_out_minutes,
            duration_minutes=duration_minutes(shift),
            active=shift.active,
            employees=employees,
            created_at=shift.created_at,
        )

    def get(self, shift_id: int) -> Shift:
        shift = self.repo.shift(shift_id)
        if shift is None:
            raise NotFoundError("Turno no encontrado", code="SHIFT_NOT_FOUND")
        return shift

    def read(self, shift_id: int) -> ShiftRead:
        shift = self.get(shift_id)
        return self._read(shift, self.repo.employees_per_shift([shift.id], business_today()).get(shift.id, 0))

    def search(self, *, search: str | None, active: bool | None, page: PageParams) -> ShiftList:
        items, total = self.repo.shifts(search=search, active=active, offset=page.offset, limit=page.size)
        employees = self.repo.employees_per_shift((s.id for s in items), business_today())
        return ShiftList.of([self._read(s, employees.get(s.id, 0)) for s in items], total, page)

    def _apply(self, shift: Shift, data: ShiftCreate) -> None:
        if self.repo.shift_name_exists(data.name, exclude_id=shift.id if shift.id else None):
            raise ConflictError(SHIFT_NAME_TAKEN, code="SHIFT_NAME_TAKEN", field="name")
        values = data.model_dump(exclude={"weekdays"})
        for field, value in values.items():
            setattr(shift, field, value)
        shift.weekdays = mask_of(data.weekdays)

    def create(self, data: ShiftCreate) -> ShiftRead:
        shift = Shift()
        self._apply(shift, data)
        self.repo.add(shift)
        self.db.commit()
        return self._read(shift, 0)

    def update(self, shift_id: int, data: ShiftUpdate) -> ShiftRead:
        shift = self.get(shift_id)
        self._apply(shift, data)
        self.db.commit()
        return self.read(shift.id)

    def set_active(self, shift_id: int, active: bool) -> ShiftRead:
        shift = self.get(shift_id)
        shift.active = active
        self.db.commit()
        return self.read(shift.id)

    def delete(self, shift_id: int) -> None:
        shift = self.get(shift_id)
        if self.repo.shift_in_use(shift.id):
            raise ConflictError(
                "El turno está asignado a empleados: desactívalo en lugar de eliminarlo", code="SHIFT_IN_USE"
            )
        self.repo.delete_shift(shift)
        self.db.commit()

    # ---------- Asignaciones ----------

    def _employee(self, employee_id: int, *, lock: bool = False) -> Employee:
        employee = EmployeeRepository(self.db, self.company_id).get_by_id(employee_id, lock=lock)
        if employee is None:
            raise NotFoundError("Empleado no encontrado", code="EMPLOYEE_NOT_FOUND")
        return employee

    def _state(self, assignment: ShiftAssignment, today: date) -> AssignmentState:
        """Vigencia (catalog.assignment_states: nombre y color para la pantalla)."""
        if assignment.valid_from > today:
            return AssignmentState.SCHEDULED
        ended = assignment.valid_to is not None and assignment.valid_to < today
        return AssignmentState.ENDED if ended else AssignmentState.CURRENT

    def _reads(self, assignments: list[ShiftAssignment]) -> list[AssignmentRead]:
        shifts = self.repo.shifts_by_ids(a.shift_id for a in assignments)
        sites = self.repo.sites_of(a.id for a in assignments)
        today = business_today()
        return [
            AssignmentRead(
                id=a.id,
                shift=shift_ref(shifts[a.shift_id]),
                valid_from=a.valid_from,
                valid_to=a.valid_to,
                remote_weekdays=weekdays_of(a.remote_weekdays),
                sites=[site_ref(site) for site in sites[a.id]],
                state=self._state(a, today),
                created_at=a.created_at,
            )
            for a in assignments
        ]

    def assignments(self, employee_id: int, page: PageParams) -> AssignmentList:
        self._employee(employee_id)
        items, total = self.repo.assignments_of(employee_id, offset=page.offset, limit=page.size)
        return AssignmentList.of(self._reads(items), total, page)

    def assign(self, employee_id: int, data: AssignmentCreate, actor: User) -> AssignmentRead:
        """El turno del empleado desde `valid_from`; con turno vigente, desde mañana o después. Si ya
        tiene exactamente esa asignación (un reintento), la devuelve sin crear otra."""
        employee = self._employee(employee_id, lock=True)  # dos cambios a la vez se atienden en orden
        _ensure_active(employee)
        shift = self._usable_shift(data)
        plan = self._plans([employee], data)[employee.id]
        if plan.error is not None:
            raise plan.error
        if plan.existing is not None:
            return self._reads([plan.existing])[0]
        assignment = self._create([employee.id], shift, data, actor)[0]
        self.db.commit()
        return self._reads([assignment])[0]

    def assign_many(self, data: AssignmentBulkCreate, actor: User) -> BulkResult:
        """El mismo turno para varios empleados, con las mismas reglas que a uno, en una transacción.

        Lo que es igual para todos (turno, sitios, días remotos, fecha pasada) rechaza la petición; lo
        de cada empleado (inactivo, cambio sin anticipación, cambio ya programado) lo omite con su
        motivo. Un id que no es de la empresa responde 404 (como si no existiera) y no asigna nada.
        Los empleados se bloquean en orden de id: dos operaciones que comparten empleados no se cruzan.
        """
        shift = self._usable_shift(data)
        if data.valid_from < business_today():
            raise _invalid(ASSIGNMENT_IN_PAST, "ASSIGNMENT_IN_PAST", "valid_from")
        employees = EmployeeRepository(self.db, self.company_id).lock_many(data.employee_ids)
        missing = set(data.employee_ids) - {employee.id for employee in employees}
        if missing:
            raise NotFoundError(
                "Algunos empleados ya no existen: actualiza la lista",
                code="EMPLOYEE_NOT_FOUND",
                details={"employee_ids": sorted(missing)},
            )
        plans = self._plans(employees, data)
        self._create([e.id for e in employees if plans[e.id].result == "DONE"], shift, data, actor)
        self.db.commit()
        ordered = sorted(employees, key=lambda e: (e.last_name.lower(), e.first_name.lower(), e.id))
        return BulkResult.of(plans[e.id].outcome(e) for e in ordered)

    def _usable_shift(self, data: AssignmentCreate) -> Shift:
        """El turno activo, los días remotos dentro de sus días y un sitio activo si algún día es en
        sitio: lo que no depende del empleado."""
        shift = self.get(data.shift_id)
        if not shift.active:
            raise _invalid("El turno está desactivado", "SHIFT_INACTIVE", "shift_id")
        remote = mask_of(data.remote_weekdays)
        if remote & ~shift.weekdays:
            names = ", ".join(WEEKDAY_NAMES[day] for day in weekdays_of(remote & ~shift.weekdays))
            raise _invalid(f"El turno no trabaja el {names}", "REMOTE_DAY_OUTSIDE_SHIFT", "remote_weekdays")
        sites = self.repo.sites_by_ids(data.site_ids)
        if len(sites) != len(set(data.site_ids)) or not all(site.active for site in sites):
            raise _invalid("Elige sitios activos de la empresa", "SITE_NOT_AVAILABLE", "site_ids")
        if shift.weekdays & ~remote and not sites:
            raise _invalid(
                "Elige al menos un sitio donde checar los días que no son remotos", "SITE_REQUIRED", "site_ids"
            )
        return shift

    def _plans(self, employees: list[Employee], data: AssignmentCreate) -> dict[int, _Plan]:
        """Qué hacer con cada empleado, con sus asignaciones leídas por lotes (sin una consulta por
        empleado): las que ya tuvo, la vigente en la fecha pedida y los cambios ya programados."""
        ids = [employee.id for employee in employees]
        day = data.valid_from
        with_shift = self.repo.employees_with_assignments(ids)
        upcoming = self.repo.assignments_from(ids, day)
        in_force = {
            employee_id: next((a for a in found if a.valid_from <= day), None)
            for employee_id, found in upcoming.items()
        }
        sites = self.repo.sites_of(a.id for a in in_force.values() if a is not None)
        wanted = _Wanted(data.shift_id, mask_of(data.remote_weekdays), frozenset(data.site_ids))
        today = business_today()
        plans: dict[int, _Plan] = {}
        for employee in employees:
            current = in_force[employee.id]
            current_sites = frozenset(site.id for site in sites[current.id]) if current else frozenset()
            plans[employee.id] = _plan(
                employee,
                day,
                today,
                has_shift=employee.id in with_shift,
                identical=current if current and wanted.matches(current, current_sites) else None,
                scheduled=next((a for a in upcoming[employee.id] if a.valid_from >= day), None),
            )
        return plans

    def _create(
        self, employee_ids: list[int], shift: Shift, data: AssignmentCreate, actor: User
    ) -> list[ShiftAssignment]:
        """Las asignaciones nuevas: la vigente de cada uno termina el día anterior; una sentencia por
        paso para todos (cerrar, insertar y sus sitios)."""
        if not employee_ids:
            return []
        self.repo.close_before(employee_ids, data.valid_from)
        assignments = self.repo.add_assignments(
            [
                ShiftAssignment(
                    employee_id=employee_id,
                    shift_id=shift.id,
                    valid_from=data.valid_from,
                    remote_weekdays=mask_of(data.remote_weekdays),
                    created_by_id=actor.id,
                )
                for employee_id in employee_ids
            ]
        )
        self.repo.set_sites(assignments, data.site_ids)
        return assignments

    def cancel(self, assignment_id: int) -> None:
        """Cancela un cambio programado (aún no empieza); la asignación anterior vuelve a no tener fin."""
        assignment = self.repo.assignment(assignment_id)
        if assignment is None:
            raise NotFoundError("Asignación no encontrada", code="ASSIGNMENT_NOT_FOUND")
        self._employee(assignment.employee_id, lock=True)
        if assignment.valid_from <= business_today():
            raise ConflictError(
                "La asignación ya empezó: para cambiar el turno, programa uno nuevo desde mañana",
                code="ASSIGNMENT_STARTED",
            )
        employee_id, previous_end = assignment.employee_id, assignment.valid_from - timedelta(days=1)
        self.repo.delete_assignment(assignment)
        self.repo.reopen_previous(employee_id, previous_end)
        self.db.commit()


@dataclass(frozen=True)
class _Wanted:
    """La asignación pedida, para reconocer una idéntica que ya rige (un reintento no duplica)."""

    shift_id: int
    remote: int
    site_ids: frozenset[int]

    def matches(self, assignment: ShiftAssignment, site_ids: frozenset[int]) -> bool:
        return (
            assignment.valid_to is None
            and assignment.shift_id == self.shift_id
            and assignment.remote_weekdays == self.remote
            and site_ids == self.site_ids
        )


@dataclass(frozen=True)
class _Plan:
    """Lo que se hará con un empleado: asignar (DONE), nada (UNCHANGED: ya la tiene) u omitirlo."""

    result: BulkResultCode
    error: AppError | None = None
    existing: ShiftAssignment | None = None

    def outcome(self, employee: Employee) -> BulkOutcome:
        return BulkOutcome.of(employee_ref(employee), self.result, self.error)


def _inactive() -> ConflictError:
    return ConflictError("El empleado está inactivo", code="EMPLOYEE_INACTIVE")


def _ensure_active(employee: Employee) -> None:
    if not employee.active:
        raise _inactive()


def _plan(
    employee: Employee,
    valid_from: date,
    today: date,
    *,
    has_shift: bool,
    identical: ShiftAssignment | None,
    scheduled: ShiftAssignment | None,
) -> _Plan:
    """Las reglas de una asignación para un empleado (las mismas para uno o para varios)."""
    if not employee.active:
        return _Plan("SKIPPED", _inactive())
    if identical is not None:
        return _Plan("UNCHANGED", existing=identical)
    if has_shift and valid_from < today + timedelta(days=ASSIGNMENT_NOTICE_DAYS):
        return _Plan(
            "SKIPPED",
            _invalid(
                "Un cambio de turno se programa con al menos un día de anticipación: elige desde mañana",
                "ASSIGNMENT_NOTICE_REQUIRED",
                "valid_from",
            ),
        )
    if valid_from < today:
        return _Plan("SKIPPED", _invalid(ASSIGNMENT_IN_PAST, "ASSIGNMENT_IN_PAST", "valid_from"))
    if scheduled is not None:
        return _Plan(
            "SKIPPED",
            ConflictError(
                f"Ya hay un cambio de turno programado desde el {scheduled.valid_from:%d/%m/%Y}: cancélalo primero",
                code="ASSIGNMENT_ALREADY_SCHEDULED",
            ),
        )
    return _Plan("DONE")
