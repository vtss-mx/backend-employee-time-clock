"""Casos de uso de administración de empleados (rol COMPANY)."""

from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.core.passwords import hash_password
from app.models import Company, Employee, EnrollmentStatus, FaceStatus, SessionRevocationReason, UserRole
from app.repositories.employee_repository import EmployeeRepository, UniqueDocument
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.repositories.qr_repository import EmployeeQrRepository
from app.repositories.user_repository import UserRepository
from app.repositories.verification_repository import VerificationLogRepository
from app.schemas.common import PageParams
from app.schemas.employee import EmployeeCreate, EmployeeList, EmployeeRead, EmployeeUpdate
from app.schemas.validators import (
    curp_birth_date_error,
    rfc_birth_date_error,
)
from app.schemas.verification import VerificationLogList, VerificationLogRead
from app.services.availability_service import CURP_TAKEN, NSS_TAKEN, NUMBER_TAKEN, RFC_TAKEN
from app.services.people_service import SHARED_ACCOUNT, PeopleService
from app.services.qr_service import QrService
from app.services.session_service import SessionService

REVERIFY_DEFAULT_REASON = "Tu empresa solicitó que verifiques nuevamente tu identidad."
DUPLICATE_MESSAGE = "El correo, teléfono, número de empleado, RFC, CURP o NSS ya está registrado"
NEW_PERSON_CREDENTIAL = "La contraseña es obligatoria para una persona nueva"
# Documentos únicos por persona: (campo, código de error, mensaje).
_UNIQUE_DOCUMENTS: tuple[tuple[UniqueDocument, str, str], ...] = (
    ("rfc", "RFC_TAKEN", RFC_TAKEN),
    ("curp", "CURP_TAKEN", CURP_TAKEN),
    ("nss", "NSS_TAKEN", NSS_TAKEN),
)
# Datos del empleado que se copian tal cual desde el alta o la edición.
_EMPLOYEE_FIELDS = (
    "first_name",
    "last_name",
    "birth_date",
    "employee_number",
    "rfc",
    "curp",
    "nss",
    "headwear_exempt",
)
# Datos de la CUENTA de la persona (compartida si trabaja en varias empresas).
_ACCOUNT_FIELDS = ("email", "phone", "password")


class EmployeeService:
    """Administración de los empleados de UNA empresa (la del COMPANY autenticado)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.users = UserRepository(db)
        self.employees = EmployeeRepository(db, company_id)
        self.faces = FaceEmbeddingRepository(db)
        self.qrs = EmployeeQrRepository(db)
        self.people = PeopleService(db, company_id)

    # ---------- Consultas ----------

    def get(self, employee_id: int) -> Employee:
        employee = self.employees.get_by_id(employee_id)
        if employee is None:
            raise NotFoundError("Empleado no encontrado", code="EMPLOYEE_NOT_FOUND")
        return employee

    def list_employees(self, *, search: str | None, active: bool | None, page: PageParams) -> EmployeeList:
        items, total = self.employees.search(search=search, active=active, offset=page.offset, limit=page.size)
        ids = [e.id for e in items]
        face_counts = self.faces.count_active_by_employee(ids)
        with_qr = self.qrs.employees_with_active_qr(ids)
        return EmployeeList.of(
            [self._to_read(e, face_counts.get(e.id, 0), e.id in with_qr) for e in items], total, page
        )

    def read(self, employee: Employee) -> EmployeeRead:
        data = self._to_read(
            employee,
            self.faces.count_active(employee.id),
            self.qrs.get_active_for_employee(employee.id) is not None,
        )
        latest = FaceEnrollmentRepository(self.db, self.company_id).latest_for_employee(employee.id)
        data.latest_enrollment_id = latest.id if latest else None
        return data

    def history(self, employee_id: int, page: PageParams) -> VerificationLogList:
        self.get(employee_id)
        logs, total = VerificationLogRepository(self.db).page_for_employee(
            employee_id, offset=page.offset, limit=page.size
        )
        return VerificationLogList.of([VerificationLogRead.model_validate(log) for log in logs], total, page)

    # ---------- Comandos ----------

    def create(self, data: EmployeeCreate) -> Employee:
        """Da de alta a una persona como empleado de esta empresa + su QR, en una sola transacción.

        Si la persona ya trabaja en otra empresa (mismo correo Y teléfono), se vincula su cuenta
        existente: entra con su misma contraseña y elige la empresa al iniciar sesión. El rostro no
        lo registra COMPANY: el empleado lo registra y COMPANY valida su identidad.
        """
        self._ensure_capacity()
        self._ensure_documents_match(data.rfc, data.curp, data.birth_date)
        self._ensure_unique(
            employee_number=data.employee_number,
            documents={"rfc": data.rfc, "curp": data.curp, "nss": data.nss},
        )
        account = self.people.account_to_link(data.email, data.phone)
        if account is None and not data.password:
            raise UnprocessableError(NEW_PERSON_CREDENTIAL, code="PASSWORD_REQUIRED", field="password")

        try:
            user = account or self.users.create(
                email=data.email,
                password_hash=hash_password(data.password or ""),
                role=UserRole.EMPLOYEE,
                phone=data.phone,
            )
            employee = self.employees.add(
                Employee(
                    user_id=user.id,
                    **data.model_dump(include=set(_EMPLOYEE_FIELDS)),
                    face_status=FaceStatus.NOT_ENROLLED,
                    active=True,
                )
            )
            QrService(self.db).issue(employee)  # QR generado automáticamente
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError(DUPLICATE_MESSAGE, code="DUPLICATE") from exc
        self.db.refresh(employee)
        self.db.refresh(employee.user)
        return employee

    def update(self, employee_id: int, data: EmployeeUpdate) -> Employee:
        employee = self.get(employee_id)
        changes = data.model_dump(exclude_unset=True, exclude_none=True)
        # RFC y CURP resultantes contra la fecha resultante (puede cambiar solo uno de los datos).
        self._ensure_documents_match(
            changes.get("rfc", employee.rfc),
            changes.get("curp", employee.curp),
            changes.get("birth_date", employee.birth_date),
        )
        self._ensure_unique(
            employee_number=changes.get("employee_number"),
            documents={field: changes.get(field) for field in ("rfc", "curp", "nss")},
            exclude_employee=employee,
        )
        self._ensure_account_changes(employee, changes)
        for field in _EMPLOYEE_FIELDS:
            if field in changes:
                setattr(employee, field, changes[field])
        if "email" in changes:
            employee.user.email = changes["email"]
        if "phone" in changes:
            employee.user.phone = changes["phone"]
        if "password" in changes:
            employee.user.password_hash = hash_password(changes["password"])
            # Cambio de contraseña: se cierran todas las sesiones del empleado.
            SessionService(self.db).revoke_all(employee.user_id, SessionRevocationReason.PASSWORD_CHANGED, commit=False)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError(DUPLICATE_MESSAGE, code="DUPLICATE") from exc
        self.db.refresh(employee)
        return employee

    def set_active(self, employee_id: int, active: bool) -> Employee:
        """Activa o desactiva el empleo en ESTA empresa (la cuenta y sus otros empleos no cambian).

        Desactivar cierra de inmediato las sesiones en las que la persona entró a esta empresa.
        """
        employee = self.get(employee_id)
        employee.active = active
        if not active:
            SessionService(self.db).revoke_company(
                employee.user_id, self.company_id, SessionRevocationReason.ACCOUNT_DEACTIVATED
            )
        self.db.commit()
        self.db.refresh(employee)
        return employee

    def delete(self, employee_id: int) -> None:
        """Eliminación definitiva del empleo en esta empresa: empleado, embeddings, QR e historial.

        La cuenta de la persona se borra solo si ya no trabaja en ninguna otra empresa.
        """
        employee = self.get(employee_id)
        user = employee.user
        others = [e for e in user.employees if e.id != employee.id]
        SessionService(self.db).revoke_company(user.id, self.company_id, SessionRevocationReason.EMPLOYEE_REMOVED)
        self.employees.delete(employee)
        if not others:
            self.db.delete(user)
        self.db.commit()

    def reset_face(self, employee_id: int, reason: str | None = None) -> Employee:
        """Solicita al empleado verificar de nuevo su identidad: elimina sus datos faciales y, en
        su próximo acceso, deberá registrar su rostro y COMPANY validarlo otra vez.

        El motivo (o uno genérico) se le muestra al empleado al entrar al registro facial.
        """
        employee = self.get(employee_id)
        for enrollment in FaceEnrollmentRepository(self.db, self.company_id).pending_for_employee(employee.id):
            enrollment.status = EnrollmentStatus.REJECTED
            enrollment.rejection_reason = "Registro reiniciado por la empresa"
            enrollment.photo_encrypted = None
        self.faces.delete_all(employee.id)
        employee.face_status = FaceStatus.NOT_ENROLLED
        employee.face_rejection_reason = (reason or "").strip() or REVERIFY_DEFAULT_REASON
        self.db.commit()
        self.db.refresh(employee)
        return employee

    # ---------- Internos ----------

    def _ensure_account_changes(self, employee: Employee, changes: dict[str, object]) -> None:
        """Correo, teléfono y contraseña son de la cuenta de la persona: únicos en la plataforma y,
        si también trabaja en otra empresa, ninguna empresa puede cambiarlos (solo ella)."""
        user = employee.user
        changed: set[str] = {f for f in ("email", "phone") if f in changes and changes[f] != getattr(user, f)}
        if "password" in changes:
            changed.add("password")
        if changed and employee.shared_account:
            raise ConflictError(SHARED_ACCOUNT, code="SHARED_ACCOUNT", field=min(changed))
        checks = {"email": self.people.check_email, "phone": self.people.check_phone}
        for field in sorted(changed & checks.keys()):
            result = checks[field](str(changes[field]), exclude_user_id=user.id)
            if result.match == "TAKEN":
                raise ConflictError(result.message or DUPLICATE_MESSAGE, code=f"{field.upper()}_TAKEN", field=field)

    def _ensure_unique(
        self,
        *,
        employee_number: str | None,
        documents: dict[str, str | None] | None = None,
        exclude_employee: Employee | None = None,
    ) -> None:
        if employee_number and self.employees.number_exists(
            employee_number, exclude_id=exclude_employee.id if exclude_employee else None
        ):
            raise ConflictError(NUMBER_TAKEN, code="EMPLOYEE_NUMBER_TAKEN", field="employee_number")
        exclude_id = exclude_employee.id if exclude_employee else None
        for field, code, message in _UNIQUE_DOCUMENTS:
            value = (documents or {}).get(field)
            if value and self.employees.unique_exists(field, value, exclude_id=exclude_id):
                raise ConflictError(message, code=code, field=field)

    def _ensure_capacity(self) -> None:
        """Límite de empleados del plan de la empresa (lo define el administrador de la plataforma)."""
        company = self.db.get(Company, self.company_id)
        if (
            company is not None
            and company.max_employees is not None
            and self.employees.count() >= company.max_employees
        ):
            raise ConflictError(
                f"Tu empresa alcanzó su límite de {company.max_employees} empleados. "
                "Contacta al administrador de la plataforma.",
                code="EMPLOYEE_LIMIT_REACHED",
            )

    @staticmethod
    def _ensure_documents_match(rfc: str | None, curp: str | None, birth_date: date) -> None:
        """RFC y CURP llevan la fecha de nacimiento: deben coincidir con la registrada."""
        rfc_error = rfc_birth_date_error(rfc, birth_date) if rfc else None
        if rfc_error:
            raise UnprocessableError(rfc_error, code="RFC_BIRTH_DATE_MISMATCH", field="rfc")
        curp_error = curp_birth_date_error(curp, birth_date) if curp else None
        if curp_error:
            raise UnprocessableError(curp_error, code="CURP_BIRTH_DATE_MISMATCH", field="curp")

    @staticmethod
    def _to_read(employee: Employee, face_count: int, has_qr: bool) -> EmployeeRead:
        return EmployeeRead(
            id=employee.id,
            user_id=employee.user_id,
            employee_number=employee.employee_number,
            first_name=employee.first_name,
            last_name=employee.last_name,
            full_name=employee.full_name,
            birth_date=employee.birth_date,
            rfc=employee.rfc,
            curp=employee.curp,
            nss=employee.nss,
            phone=employee.user.phone,
            email=employee.user.email,
            shared_account=employee.shared_account,
            active=employee.active,
            headwear_exempt=employee.headwear_exempt,
            face_status=employee.face_status,
            face_rejection_reason=employee.face_rejection_reason,
            has_face=face_count > 0,
            face_samples=face_count,
            has_active_qr=has_qr,
            created_at=employee.created_at,
            updated_at=employee.updated_at,
        )
