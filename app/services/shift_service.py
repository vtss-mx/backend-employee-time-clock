"""Turnos de una empresa y su asignación a los empleados.

El turno dice DÓNDE y CUÁNDO se checa (decisión del dueño del producto, migración 0048): horario,
días, descansos, tolerancias, sus sitios y los días en que se puede checar remoto. Regla del lugar:
los días remotos son días del turno (`REMOTE_DAY_OUTSIDE_SHIFT`; también un CHECK de la base) y, si
algún día del turno no es remoto, el turno necesita al menos un sitio (`SITE_REQUIRED`; un CHECK no
puede ver otra tabla, por eso vive aquí). Un sitio nuevo en el turno debe estar activo
(`SITE_NOT_AVAILABLE`); uno que ya tenía y luego se desactivó puede quedarse hasta que se quite (no
acepta registros mientras siga inactivo).

Asignar es solo elegir el turno y desde cuándo. Cambiar el turno de un empleado nunca toca lo ya
registrado:
- Si ya tiene turno, el nuevo empieza mañana o después (`ASSIGNMENT_NOTICE_DAYS`): la asignación
  vigente termina el día anterior y las jornadas ya registradas conservan el turno con que ocurrieron
  (`WorkSession` guarda su horario). Solo la primera asignación puede empezar hoy.
- Un cambio ya programado se puede cancelar mientras no empiece; la asignación anterior recupera su
  vigencia.
- Las asignaciones no se enciman: un empleado tiene a lo más un turno cada día (el empleado se lee
  con candado de fila para que dos cambios simultáneos no se crucen).

Editar un turno (también sus sitios o sus días remotos) aplica desde ese momento a todos los que lo
tienen asignado; las jornadas registradas guardan su copia de lo programado.

Eliminar un turno y cancelar un cambio programado son borrados lógicos (regla 20 de la raíz): van a «Eliminados» y se
pueden restaurar; restaurar un turno exige su nombre libre y sus sitios vigentes, y restaurar un cambio programado
vuelve a aplicar las MISMAS reglas que asignar (`_plans`).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.exceptions import AppError, ConflictError, NotFoundError, UnprocessableError
from app.i18n import Params, Text
from app.models import AssignmentState, Employee, Shift, ShiftAssignment, User, WorkSite
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.shift_repository import ShiftRepository
from app.schemas.bulk import BulkOutcome, BulkResult, BulkResultCode
from app.schemas.common import EmployeeRef, PageParams, deletion_of
from app.schemas.shift import (
    AssignmentBulkCreate,
    AssignmentCreate,
    AssignmentList,
    AssignmentRead,
    ShiftCreate,
    ShiftList,
    ShiftRead,
    ShiftRef,
    ShiftSummary,
    ShiftUpdate,
)
from app.services.shift_rules import WEEKDAY_NAMES, duration_minutes, mask_of, overnight, weekdays_of
from app.services.site_service import site_ref
from app.services.trash import commit_restore, ensure_deleted, ensure_live, ensure_name_free

#: Un cambio de turno se programa con al menos un día de anticipación.
ASSIGNMENT_NOTICE_DAYS = 1


def employee_ref(employee: Employee) -> EmployeeRef:
    return EmployeeRef(
        id=employee.id,
        full_name=employee.full_name,
        employee_number=employee.employee_number,
        deleted=employee.deleted,
    )


def _ref_fields(shift: Shift) -> dict[str, Any]:
    """Los campos de `ShiftRef` (sin validar de nuevo lo que ya viene de la base)."""
    return {
        "id": shift.id,
        "name": shift.name,
        "start_time": shift.start_time,
        "end_time": shift.end_time,
        "overnight": overnight(shift),
        "weekdays": weekdays_of(shift.weekdays),
        "remote_weekdays": weekdays_of(shift.remote_weekdays),
        "deleted": shift.deleted,
    }


def shift_ref(shift: Shift) -> ShiftRef:
    return ShiftRef(**_ref_fields(shift))


def shift_summary(shift: Shift, sites: list[WorkSite]) -> ShiftSummary:
    """El turno con dónde se checa (sus sitios ya leídos por lotes)."""
    return ShiftSummary(**_ref_fields(shift), sites=[site_ref(site) for site in sites])


def _invalid(code: str, field: str, params: Params | None = None) -> UnprocessableError:
    """422 con el mensaje de su código (catálogo de mensajes) en el campo del formulario."""
    return UnprocessableError(code=code, params=params, field=field)


class ShiftService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = ShiftRepository(db, company_id)

    # ---------- Turnos ----------

    @staticmethod
    def _read(shift: Shift, sites: list[WorkSite], employees: int) -> ShiftRead:
        return ShiftRead(
            **_ref_fields(shift),
            sites=[site_ref(site) for site in sites],
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
            **deletion_of(shift),
        )

    def get(self, shift_id: int, *, include_deleted: bool = False) -> Shift:
        """El vigente (404 si no existe o está en «Eliminados»); con `include_deleted`, también uno eliminado."""
        shift = self.repo.shift(shift_id, include_deleted=include_deleted)
        if shift is None:
            raise NotFoundError(code="SHIFT_NOT_FOUND")
        return shift

    def read(self, shift_id: int, *, include_deleted: bool = False) -> ShiftRead:
        shift = self.get(shift_id, include_deleted=include_deleted)
        employees = self.repo.employees_per_shift([shift.id], business_today()).get(shift.id, 0)
        return self._read(shift, self.repo.sites_of_shifts([shift.id])[shift.id], employees)

    def search(self, *, search: str | None, active: bool | None, page: PageParams, deleted: bool = False) -> ShiftList:
        """Una página de turnos con sus sitios y cuántos lo tienen hoy (una consulta para cada cosa); con `deleted`,
        la papelera (el eliminado más reciente primero)."""
        items, total = self.repo.shifts(
            search=search, active=active, offset=page.offset, limit=page.size, deleted=deleted
        )
        ids = [s.id for s in items]
        employees = self.repo.employees_per_shift(ids, business_today())
        sites = self.repo.sites_of_shifts(ids)
        return ShiftList.of([self._read(s, sites[s.id], employees.get(s.id, 0)) for s in items], total, page)

    def _place(self, data: ShiftCreate, current: set[int]) -> None:
        """Dónde se checa con el turno: días remotos dentro de sus días, sitios de la empresa (los nuevos,
        activos) y al menos uno si algún día no es remoto."""
        weekdays, remote = mask_of(data.weekdays), mask_of(data.remote_weekdays)
        if remote & ~weekdays:
            days = [Text(WEEKDAY_NAMES[day]) for day in weekdays_of(remote & ~weekdays)]
            raise _invalid("REMOTE_DAY_OUTSIDE_SHIFT", "remote_weekdays", {"days": days})
        sites = self.repo.sites_by_ids(data.site_ids)
        if len(sites) != len(data.site_ids) or any(not site.active and site.id not in current for site in sites):
            raise _invalid("SITE_NOT_AVAILABLE", "site_ids")
        if weekdays & ~remote and not sites:
            raise _invalid("SITE_REQUIRED", "site_ids")

    def _apply(self, shift: Shift, data: ShiftCreate) -> None:
        """Valida y copia el turno completo; sus sitios quedan exactamente los pedidos."""
        if self.repo.shift_name_exists(data.name, exclude_id=shift.id if shift.id else None):
            raise ConflictError(code="SHIFT_NAME_TAKEN", field="name")
        current = self.repo.shift_site_ids(shift.id) if shift.id else set()
        self._place(data, current)
        values = data.model_dump(exclude={"weekdays", "remote_weekdays", "site_ids"})
        for field, value in values.items():
            setattr(shift, field, value)
        shift.weekdays = mask_of(data.weekdays)
        shift.remote_weekdays = mask_of(data.remote_weekdays)
        if not shift.id:
            self.repo.add(shift)
        self.repo.set_shift_sites(shift.id, current, set(data.site_ids))

    def create(self, data: ShiftCreate) -> ShiftRead:
        shift = Shift()
        self._apply(shift, data)
        self.db.commit()
        return self._read(shift, self.repo.sites_of_shifts([shift.id])[shift.id], 0)

    def update(self, shift_id: int, data: ShiftUpdate) -> ShiftRead:
        """Aplica desde ahora a todos los que lo tienen asignado (lo registrado guarda su copia)."""
        shift = self.get(shift_id)
        self._apply(shift, data)
        self.db.commit()
        return self.read(shift.id)

    def set_active(self, shift_id: int, active: bool) -> ShiftRead:
        shift = self.get(shift_id)
        shift.active = active
        self.db.commit()
        return self.read(shift.id)

    def delete(self, shift_id: int, actor: User) -> None:
        """A «Eliminados», solo un turno que nadie tiene asignado (409 `SHIFT_IN_USE`: desactívalo). Sus solicitudes
        de cambio pendientes se cancelan; su nombre queda libre."""
        shift = self.get(shift_id, include_deleted=True)
        ensure_live(shift)
        if self.repo.shift_in_use(shift.id):
            raise ConflictError(code="SHIFT_IN_USE")
        now = datetime.now(UTC)
        self.repo.cancel_pending_requests(actor_id=actor.id, now=now, shift_id=shift.id)
        shift.mark_deleted(now, actor.email)
        self.db.commit()

    def restore(self, shift_id: int) -> ShiftRead:
        """Regresa de «Eliminados» con su estado de antes si su nombre sigue libre y sus sitios siguen vigentes (409
        `RESTORE_CONFLICT`: un sitio eliminado se restaura primero). Sus solicitudes canceladas no regresan."""
        shift = self.get(shift_id, include_deleted=True)
        ensure_deleted(shift)
        ensure_name_free(self.repo.shift_name_exists(shift.name), shift.name)
        sites = self.repo.deleted_sites_of(shift.id)
        if sites:
            raise ConflictError(
                code="RESTORE_CONFLICT",
                key="RESTORE_SITES_DELETED",
                params={"count": len(sites), "sites": sites},
                field="site_ids",
            )
        shift.mark_restored()
        commit_restore(self.db)
        return self.read(shift.id)

    # ---------- Asignaciones ----------

    def _employee(self, employee_id: int, *, lock: bool = False) -> Employee:
        employee = EmployeeRepository(self.db, self.company_id).get_by_id(employee_id, lock=lock)
        if employee is None:
            raise NotFoundError(code="EMPLOYEE_NOT_FOUND")
        return employee

    def _state(self, assignment: ShiftAssignment, today: date) -> AssignmentState:
        """Vigencia (catalog.assignment_states: nombre y color para la pantalla)."""
        if assignment.valid_from > today:
            return AssignmentState.SCHEDULED
        ended = assignment.valid_to is not None and assignment.valid_to < today
        return AssignmentState.ENDED if ended else AssignmentState.CURRENT

    def _reads(self, assignments: list[ShiftAssignment]) -> list[AssignmentRead]:
        """Cada asignación con su turno y dónde se checa (turnos y sitios por lotes)."""
        shifts = self.repo.shifts_by_ids(a.shift_id for a in assignments)
        sites = self.repo.sites_of_shifts(shifts)
        today = business_today()
        return [
            AssignmentRead(
                id=a.id,
                shift=shift_summary(shifts[a.shift_id], sites[a.shift_id]),
                valid_from=a.valid_from,
                valid_to=a.valid_to,
                state=self._state(a, today),
                created_at=a.created_at,
                **deletion_of(a),
            )
            for a in assignments
        ]

    def assignments(self, employee_id: int, page: PageParams, *, deleted: bool = False) -> AssignmentList:
        """Las asignaciones del empleado o, con `deleted`, sus cambios cancelados (papelera)."""
        self._employee(employee_id)
        items, total = self.repo.assignments_of(employee_id, offset=page.offset, limit=page.size, deleted=deleted)
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

        Lo que es igual para todos (el turno, la fecha pasada) rechaza la petición; lo de cada empleado
        (inactivo, cambio sin anticipación, cambio ya programado) lo omite con su motivo. Un id que no es
        de la empresa responde 404 (como si no existiera) y no asigna nada.
        Los empleados se bloquean en orden de id: dos operaciones que comparten empleados no se cruzan.
        """
        shift = self._usable_shift(data)
        if data.valid_from < business_today():
            raise _invalid("ASSIGNMENT_IN_PAST", "valid_from")
        employees = EmployeeRepository(self.db, self.company_id).lock_many(data.employee_ids)
        missing = set(data.employee_ids) - {employee.id for employee in employees}
        if missing:
            raise NotFoundError(
                code="EMPLOYEE_NOT_FOUND",
                key="EMPLOYEES_GONE",
                details={"employee_ids": sorted(missing)},
            )
        plans = self._plans(employees, data)
        self._create([e.id for e in employees if plans[e.id].result == "DONE"], shift, data, actor)
        self.db.commit()
        ordered = sorted(employees, key=lambda e: (e.last_name.lower(), e.first_name.lower(), e.id))
        return BulkResult.of(plans[e.id].outcome(e) for e in ordered)

    def _usable_shift(self, data: AssignmentCreate) -> Shift:
        """El turno existe y está activo: lo que no depende del empleado (dónde checa ya lo validó el
        turno)."""
        shift = self.get(data.shift_id)
        if not shift.active:
            raise _invalid("SHIFT_INACTIVE", "shift_id")
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
        today = business_today()
        plans: dict[int, _Plan] = {}
        for employee in employees:
            current = in_force[employee.id]
            plans[employee.id] = _plan(
                employee,
                day,
                today,
                has_shift=employee.id in with_shift,
                identical=current if current and _same(current, data.shift_id) else None,
                scheduled=next((a for a in upcoming[employee.id] if a.valid_from >= day), None),
            )
        return plans

    def _create(
        self, employee_ids: list[int], shift: Shift, data: AssignmentCreate, actor: User
    ) -> list[ShiftAssignment]:
        """Las asignaciones nuevas: la vigente de cada uno termina el día anterior; una sentencia por
        paso para todos (cerrar e insertar)."""
        if not employee_ids:
            return []
        self.repo.close_before(employee_ids, data.valid_from)
        return self.repo.add_assignments(
            [
                ShiftAssignment(
                    employee_id=employee_id, shift_id=shift.id, valid_from=data.valid_from, created_by_id=actor.id
                )
                for employee_id in employee_ids
            ]
        )

    def _assignment(self, assignment_id: int) -> ShiftAssignment:
        """Vigente o cancelada (en «Eliminados»): 404 solo si no existe o es de otra empresa."""
        assignment = self.repo.assignment(assignment_id, include_deleted=True)
        if assignment is None:
            raise NotFoundError(code="ASSIGNMENT_NOT_FOUND")
        return assignment

    def cancel(self, assignment_id: int, actor: User) -> None:
        """Cancela un cambio programado (aún no empieza): va a «Eliminados» y la asignación anterior vuelve a no
        tener fin."""
        assignment = self._assignment(assignment_id)
        ensure_live(assignment)
        self._employee(assignment.employee_id, lock=True)
        if assignment.valid_from <= business_today():
            raise ConflictError(
                code="ASSIGNMENT_STARTED",
            )
        self.repo.reopen_previous(assignment.employee_id, assignment.valid_from - timedelta(days=1))
        assignment.mark_deleted(datetime.now(UTC), actor.email)
        self.db.commit()

    def restore_assignment(self, assignment_id: int) -> AssignmentRead:
        """Vuelve a programar un cambio cancelado con las MISMAS reglas que asignarlo (`_plans`): el empleado y el
        turno vigentes y activos, la fecha aún por venir con su anticipación y ningún otro cambio programado (cada
        rechazo con su código). La asignación anterior vuelve a terminar el día previo."""
        assignment = self._assignment(assignment_id)
        ensure_deleted(assignment)
        employee = EmployeeRepository(self.db, self.company_id).get_by_id(assignment.employee_id, lock=True)
        if employee is None:
            raise ConflictError(code="RESTORE_CONFLICT", key="RESTORE_EMPLOYEE_DELETED")
        if self.repo.shift(assignment.shift_id) is None:
            raise ConflictError(code="RESTORE_CONFLICT", key="RESTORE_SHIFT_DELETED", field="shift_id")
        data = AssignmentCreate.model_construct(shift_id=assignment.shift_id, valid_from=assignment.valid_from)
        self._usable_shift(data)
        plan = self._plans([employee], data)[employee.id]
        if plan.error is not None:
            raise plan.error
        if plan.existing is not None:
            raise ConflictError(code="RESTORE_CONFLICT", key="RESTORE_ASSIGNMENT_CURRENT")
        self.repo.close_before([employee.id], assignment.valid_from)
        assignment.mark_restored()
        self.db.commit()
        return self._reads([assignment])[0]


def _same(assignment: ShiftAssignment, shift_id: int) -> bool:
    """Ya rige sin fin con ese turno: la asignación pedida es idéntica (un reintento no duplica)."""
    return assignment.valid_to is None and assignment.shift_id == shift_id


@dataclass(frozen=True)
class _Plan:
    """Lo que se hará con un empleado: asignar (DONE), nada (UNCHANGED: ya la tiene) u omitirlo."""

    result: BulkResultCode
    error: AppError | None = None
    existing: ShiftAssignment | None = None

    def outcome(self, employee: Employee) -> BulkOutcome:
        return BulkOutcome.of(employee_ref(employee), self.result, self.error)


def _inactive() -> ConflictError:
    return ConflictError(code="EMPLOYEE_INACTIVE")


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
            _invalid("ASSIGNMENT_NOTICE_REQUIRED", "valid_from"),
        )
    if valid_from < today:
        return _Plan("SKIPPED", _invalid("ASSIGNMENT_IN_PAST", "valid_from"))
    if scheduled is not None:
        return _Plan(
            "SKIPPED",
            ConflictError(code="ASSIGNMENT_ALREADY_SCHEDULED", params={"date": scheduled.valid_from}),
        )
    return _Plan("DONE")
