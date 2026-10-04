"""Ausencias de los empleados: vacaciones, permisos, incapacidades y otros días libres.

- La empresa las registra directamente (aprobadas de una vez) para uno o varios empleados a la vez
  (vacaciones colectivas), con las reglas de una operación masiva: una transacción, empleados
  bloqueados en orden de id y un resultado por empleado (registrada, ya la tenía u omitida).
- El empleado pide vacaciones o un permiso (los tipos `requestable` del catálogo) desde "Mi
  asistencia": queda pendiente hasta que la empresa la aprueba o la rechaza (con su motivo), como un
  cambio de turno. Puede cancelar su solicitud mientras siga pendiente.
- Dos ausencias pendientes o aprobadas del mismo empleado no se enciman (se revisa con el empleado
  bloqueado; el índice parcial de las activas hace barata la consulta).
- Una ausencia aprobada vuelve libres sus días: no se puede checar (`attendance_service`) y el tablero
  muestra "Día libre", no "Faltó".
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.exceptions import AppError, ConflictError, NotFoundError, UnprocessableError
from app.models import Employee, EmployeeAbsence, ShiftRequestStatus, User
from app.repositories.calendar_repository import ACTIVE_STATUSES, CalendarRepository
from app.repositories.employee_repository import EmployeeRepository
from app.schemas.bulk import BulkOutcome, BulkResult, BulkResultCode
from app.schemas.calendar import (
    AbsenceCreate,
    AbsenceList,
    AbsenceRead,
    AbsenceReject,
    AbsenceRequest,
)
from app.schemas.common import PageParams
from app.services.calendar_rules import span_days
from app.services.catalog_service import get_catalogs
from app.services.shift_service import employee_ref

ABSENCE_NOT_FOUND = "Ausencia no encontrada"


def _overlap(absence: EmployeeAbsence) -> ConflictError:
    catalogs = get_catalogs()
    kind = catalogs.name("day_off_types", absence.type_code)
    state = catalogs.name("shift_request_statuses", absence.status).lower()
    return ConflictError(
        f"Se encima con su ausencia «{kind}» ({state}) del {absence.starts_on:%d/%m/%Y} al {absence.ends_on:%d/%m/%Y}",
        code="ABSENCE_OVERLAP",
        details={"absence_id": absence.id},
    )


class AbsenceService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = CalendarRepository(db, company_id)
        self.employees = EmployeeRepository(db, company_id)

    def _reads(self, absences: list[EmployeeAbsence]) -> list[AbsenceRead]:
        employees = self.employees.by_ids({a.employee_id for a in absences})
        return [
            AbsenceRead(
                id=a.id,
                employee=employee_ref(employees[a.employee_id]),
                type=a.type_code,
                starts_on=a.starts_on,
                ends_on=a.ends_on,
                days=span_days(a.starts_on, a.ends_on),
                note=a.note,
                status=a.status,
                requested_by_employee=a.requested_by_id == employees[a.employee_id].user_id,
                decided_at=a.decided_at,
                decision_note=a.decision_note,
                created_at=a.created_at,
            )
            for a in absences
        ]

    @staticmethod
    def _ensure_type(type_code: str, *, requestable: bool) -> None:
        """El tipo existe y está activo en el catálogo; el empleado solo pide los que se pueden pedir."""
        row = get_catalogs().get("day_off_types", type_code)
        if row is None or not row["active"]:
            raise UnprocessableError("Elige un tipo de ausencia válido", code="DAY_OFF_TYPE_INVALID", field="type")
        if requestable and not row["requestable"]:
            raise UnprocessableError(
                f"«{row['name']}» lo registra tu empresa: pídeselo directamente",
                code="DAY_OFF_TYPE_NOT_REQUESTABLE",
                field="type",
            )

    # ---------- Empresa ----------

    def search(
        self,
        *,
        employee_id: int | None,
        type_code: str | None,
        status: str | None,
        start: date | None,
        end: date | None,
        page: PageParams,
    ) -> AbsenceList:
        items, total = self.repo.absences(
            employee_id=employee_id,
            type_code=type_code,
            status=status,
            start=start,
            end=end,
            offset=page.offset,
            limit=page.size,
        )
        return AbsenceList.of(self._reads(items), total, page)

    def pending(self) -> int:
        return self.repo.pending_absences()

    def create_many(self, data: AbsenceCreate, actor: User) -> BulkResult:
        """La misma ausencia (aprobada) para varios empleados. Cada uno: registrada, sin cambios (ya
        tenía exactamente esa: un reintento no duplica) u omitida (inactivo o se encima con otra)."""
        self._ensure_type(data.type, requestable=False)
        employees = self.employees.lock_many(data.employee_ids)
        missing = set(data.employee_ids) - {employee.id for employee in employees}
        if missing:
            raise NotFoundError(
                "Algunos empleados ya no existen: actualiza la lista",
                code="EMPLOYEE_NOT_FOUND",
                details={"employee_ids": sorted(missing)},
            )
        active = self.repo.active_overlapping(data.employee_ids, data.starts_on, data.ends_on)
        outcomes: dict[int, tuple[BulkResultCode, AppError | None]] = {}
        new: list[EmployeeAbsence] = []
        now = datetime.now(UTC)
        for employee in employees:
            outcomes[employee.id] = _outcome(employee, data, active[employee.id])
            if outcomes[employee.id][0] == "DONE":
                new.append(self._approved(employee, data, actor, now))
        self.repo.insert_absences(new)
        self.db.commit()
        ordered = sorted(employees, key=lambda e: (e.last_name.lower(), e.first_name.lower(), e.id))
        return BulkResult.of(BulkOutcome.of(employee_ref(e), *outcomes[e.id]) for e in ordered)

    @staticmethod
    def _approved(employee: Employee, data: AbsenceCreate, actor: User, now: datetime) -> EmployeeAbsence:
        return EmployeeAbsence(
            employee_id=employee.id,
            type_code=data.type,
            starts_on=data.starts_on,
            ends_on=data.ends_on,
            note=data.note,
            status=ShiftRequestStatus.APPROVED,
            requested_by_id=actor.id,
            decided_by_id=actor.id,
            decided_at=now,
        )

    def approve(self, absence_id: int, actor: User) -> AbsenceRead:
        """Aprueba una solicitud pendiente: sus días quedan libres (si no se encima con otra aprobada)."""
        absence = self._get(absence_id)
        self.employees.get_by_id(absence.employee_id, lock=True)  # en orden con otras ausencias suyas
        absence = self._get(absence_id, lock=True)
        self._ensure_pending(absence)
        others = self.repo.active_overlapping([absence.employee_id], absence.starts_on, absence.ends_on)
        clash = next(
            (a for a in others[absence.employee_id] if a.id != absence.id and a.status == ShiftRequestStatus.APPROVED),
            None,
        )
        if clash is not None:
            raise _overlap(clash)
        return self._decide(absence, ShiftRequestStatus.APPROVED, actor, None)

    def reject(self, absence_id: int, data: AbsenceReject, actor: User) -> AbsenceRead:
        absence = self._get(absence_id, lock=True)
        self._ensure_pending(absence)
        return self._decide(absence, ShiftRequestStatus.REJECTED, actor, " ".join(data.note.split()))

    def cancel(self, absence_id: int, actor: User) -> AbsenceRead:
        """La empresa retira una ausencia pendiente o aprobada: sus días vuelven a ser laborables."""
        absence = self._get(absence_id, lock=True)
        if absence.status not in ACTIVE_STATUSES:
            raise ConflictError("La ausencia ya no está vigente", code="ABSENCE_CLOSED")
        return self._decide(absence, ShiftRequestStatus.CANCELLED, actor, None)

    # ---------- Empleado ----------

    def mine(self, employee: Employee, page: PageParams) -> AbsenceList:
        items, total = self.repo.absences(
            employee_id=employee.id,
            type_code=None,
            status=None,
            start=None,
            end=None,
            offset=page.offset,
            limit=page.size,
        )
        return AbsenceList.of(self._reads(items), total, page)

    def request(self, employee: Employee, data: AbsenceRequest, user: User) -> AbsenceRead:
        """Pide vacaciones o un permiso desde hoy en adelante; queda pendiente de la empresa."""
        self._ensure_type(data.type, requestable=True)
        if data.starts_on < business_today():
            raise UnprocessableError("Pide tus días desde hoy en adelante", code="ABSENCE_IN_PAST", field="starts_on")
        self.employees.get_by_id(employee.id, lock=True)  # dos solicitudes a la vez se revisan en orden
        clash = next(iter(self.repo.active_overlapping([employee.id], data.starts_on, data.ends_on)[employee.id]), None)
        if clash is not None:
            raise _overlap(clash)
        absence = EmployeeAbsence(
            employee_id=employee.id,
            type_code=data.type,
            starts_on=data.starts_on,
            ends_on=data.ends_on,
            note=data.note,
            status=ShiftRequestStatus.PENDING,
            requested_by_id=user.id,
        )
        self.repo.add_all([absence])
        self.db.commit()
        return self._reads([absence])[0]

    def cancel_mine(self, employee: Employee, absence_id: int) -> AbsenceRead:
        """El empleado retira su solicitud mientras siga pendiente (una ausencia ajena no existe)."""
        absence = self._get(absence_id, lock=True)
        if absence.employee_id != employee.id:
            raise NotFoundError(ABSENCE_NOT_FOUND, code="ABSENCE_NOT_FOUND")
        self._ensure_pending(absence)
        absence.status = ShiftRequestStatus.CANCELLED
        self.db.commit()
        return self._reads([absence])[0]

    # ---------- Internos ----------

    def _get(self, absence_id: int, *, lock: bool = False) -> EmployeeAbsence:
        absence = self.repo.absence(absence_id, lock=lock)
        if absence is None:
            raise NotFoundError(ABSENCE_NOT_FOUND, code="ABSENCE_NOT_FOUND")
        return absence

    @staticmethod
    def _ensure_pending(absence: EmployeeAbsence) -> None:
        if absence.status != ShiftRequestStatus.PENDING:
            raise ConflictError("La solicitud ya fue atendida", code="ABSENCE_CLOSED")

    def _decide(
        self, absence: EmployeeAbsence, status: ShiftRequestStatus, actor: User, note: str | None
    ) -> AbsenceRead:
        absence.status = status
        absence.decided_by_id = actor.id
        absence.decided_at = datetime.now(UTC)
        absence.decision_note = note
        self.db.commit()
        return self._reads([absence])[0]


def _outcome(
    employee: Employee, data: AbsenceCreate, active: Iterable[EmployeeAbsence]
) -> tuple[BulkResultCode, AppError | None]:
    """Las reglas de una ausencia colectiva para un empleado."""
    if not employee.active:
        return "SKIPPED", ConflictError("El empleado está inactivo", code="EMPLOYEE_INACTIVE")
    found = list(active)
    same = (data.type, data.starts_on, data.ends_on, ShiftRequestStatus.APPROVED)
    if any((a.type_code, a.starts_on, a.ends_on, a.status) == same for a in found):
        return "UNCHANGED", None
    if found:
        return "SKIPPED", _overlap(found[0])
    return "DONE", None
