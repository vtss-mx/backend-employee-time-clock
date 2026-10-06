"""Solicitudes de cambio de turno: el empleado pide otro turno desde una fecha y la empresa decide.

- El empleado tiene a lo más una solicitud pendiente (índice único parcial) y la puede cancelar.
- Pedir y aprobar exige al menos un día de anticipación (`ASSIGNMENT_NOTICE_DAYS`): si al aprobar
  la fecha pedida ya no la cumple, la empresa elige otra fecha.
- Aprobar programa la asignación con las mismas reglas que un cambio hecho por la empresa
  (`ShiftService.assign`). Dónde checa lo dice el turno pedido (sus sitios y días remotos): la
  empresa solo puede ajustar la fecha.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models import Employee, ShiftChangeRequest, ShiftRequestStatus, User
from app.repositories.shift_repository import ShiftRepository
from app.schemas.common import PageParams
from app.schemas.shift import (
    AssignmentCreate,
    ShiftRequestApprove,
    ShiftRequestCreate,
    ShiftRequestList,
    ShiftRequestRead,
    ShiftRequestReject,
)
from app.services.shift_service import ASSIGNMENT_NOTICE_DAYS, ShiftService, employee_ref, shift_ref, shift_summary


class ShiftRequestService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = ShiftRepository(db, company_id)
        self.shifts = ShiftService(db, company_id)

    def _reads(self, requests: list[ShiftChangeRequest]) -> list[ShiftRequestRead]:
        """Cada solicitud con el turno pedido (y dónde se checa) y el que tiene hoy: todo por lotes."""
        employees = self.repo.employees_by_ids(r.employee_id for r in requests)
        shifts = self.repo.shifts_by_ids(r.shift_id for r in requests)
        sites = self.repo.sites_of_shifts(shifts)
        current = self.repo.current_assignments({r.employee_id for r in requests}, business_today())
        current_shifts = self.repo.shifts_by_ids(a.shift_id for a in current.values())
        return [
            ShiftRequestRead(
                id=r.id,
                employee=employee_ref(employees[r.employee_id]),
                shift=shift_summary(shifts[r.shift_id], sites[r.shift_id]),
                current_shift=shift_ref(current_shifts[a.shift_id]) if (a := current.get(r.employee_id)) else None,
                valid_from=r.valid_from,
                reason=r.reason,
                status=r.status,
                review_note=r.review_note,
                reviewed_at=r.reviewed_at,
                created_at=r.created_at,
            )
            for r in requests
        ]

    def _ensure_notice(self, valid_from_ok: bool) -> None:
        if not valid_from_ok:
            raise UnprocessableError(
                code="SHIFT_REQUEST_NOTICE_REQUIRED",
                field="valid_from",
            )

    # ---------- Empleado ----------

    def mine(self, employee: Employee, page: PageParams) -> ShiftRequestList:
        items, total = self.repo.requests(status=None, employee_id=employee.id, offset=page.offset, limit=page.size)
        return ShiftRequestList.of(self._reads(items), total, page)

    def create(self, employee: Employee, data: ShiftRequestCreate) -> ShiftRequestRead:
        shift = self.shifts.get(data.shift_id)
        if not shift.active:
            raise UnprocessableError(code="SHIFT_INACTIVE", key="SHIFT_NOT_AVAILABLE", field="shift_id")
        self._ensure_notice(data.valid_from >= business_today() + timedelta(days=ASSIGNMENT_NOTICE_DAYS))
        if self.repo.has_pending_request(employee.id):
            raise ConflictError(code="SHIFT_REQUEST_PENDING")
        request = ShiftChangeRequest(
            employee_id=employee.id, shift_id=shift.id, valid_from=data.valid_from, reason=data.reason
        )
        try:
            self.repo.add(request)
            self.db.commit()
        except IntegrityError as exc:  # otra solicitud pendiente creada al mismo tiempo
            self.db.rollback()
            raise ConflictError(code="SHIFT_REQUEST_PENDING") from exc
        return self._reads([request])[0]

    def cancel(self, employee: Employee, request_id: int) -> ShiftRequestRead:
        request = self._get(request_id)
        if request.employee_id != employee.id:
            raise NotFoundError(code="SHIFT_REQUEST_NOT_FOUND")
        self._ensure_pending(request)
        request.status = ShiftRequestStatus.CANCELLED
        self.db.commit()
        return self._reads([request])[0]

    # ---------- Empresa ----------

    def inbox(self, *, status: str | None, page: PageParams) -> ShiftRequestList:
        items, total = self.repo.requests(status=status, employee_id=None, offset=page.offset, limit=page.size)
        return ShiftRequestList.of(self._reads(items), total, page)

    def pending(self) -> int:
        return self.repo.pending_requests()

    def approve(self, request_id: int, data: ShiftRequestApprove, reviewer: User) -> ShiftRequestRead:
        request = self._get(request_id, lock=True)
        self._ensure_pending(request)
        valid_from = data.valid_from or request.valid_from
        self._ensure_notice(valid_from >= business_today() + timedelta(days=ASSIGNMENT_NOTICE_DAYS))
        assignment = AssignmentCreate(shift_id=request.shift_id, valid_from=valid_from)
        self.shifts.assign(request.employee_id, assignment, reviewer)
        return self._review(request, ShiftRequestStatus.APPROVED, reviewer, None)

    def reject(self, request_id: int, data: ShiftRequestReject, reviewer: User) -> ShiftRequestRead:
        request = self._get(request_id, lock=True)
        self._ensure_pending(request)
        return self._review(request, ShiftRequestStatus.REJECTED, reviewer, " ".join(data.note.split()))

    # ---------- Internos ----------

    def _get(self, request_id: int, *, lock: bool = False) -> ShiftChangeRequest:
        request = self.repo.request(request_id, lock=lock)
        if request is None:
            raise NotFoundError(code="SHIFT_REQUEST_NOT_FOUND")
        return request

    @staticmethod
    def _ensure_pending(request: ShiftChangeRequest) -> None:
        if request.status != ShiftRequestStatus.PENDING:
            raise ConflictError(code="SHIFT_REQUEST_CLOSED")

    def _review(
        self, request: ShiftChangeRequest, status: ShiftRequestStatus, reviewer: User, note: str | None
    ) -> ShiftRequestRead:
        request.status = status
        request.reviewed_by_id = reviewer.id
        request.reviewed_at = datetime.now(UTC)
        request.review_note = note
        self.db.commit()
        return self._reads([request])[0]
