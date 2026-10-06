"""Casos de uso de administración de empleados (rol COMPANY).

Eliminar es un borrado lógico (regla 20 de la raíz, `app/core/soft_delete.py`): el empleado va a «Eliminados» con su
historial intacto y se puede restaurar durante `SOFT_DELETE_RETENTION_DAYS`; sus datos biométricos y fotos se borran
de verdad en ese momento (`person_erasure`) y restaurarlo no los regresa.
"""

from datetime import UTC, date, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.core.passwords import hash_password
from app.i18n import stored
from app.models import Company, Employee, FaceStatus, SessionRevocationReason, User, UserRole
from app.repositories.billing_repository import BillingRepository
from app.repositories.calendar_repository import CalendarRepository
from app.repositories.department_repository import DepartmentRepository
from app.repositories.employee_repository import EmployeeRepository, UniqueDocument
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.repositories.face_repository import NO_SAMPLES, FaceEmbeddingRepository, SampleStats
from app.repositories.qr_repository import EmployeeQrRepository
from app.repositories.shift_repository import ShiftRepository
from app.repositories.user_repository import UserRepository
from app.repositories.verification_repository import VerificationLogRepository
from app.schemas.avatar import avatar_path
from app.schemas.bulk import BULK_MAX
from app.schemas.common import PageParams, deletion_of
from app.schemas.employee import (
    OPTIONAL_DOCUMENTS,
    DepartmentRef,
    EmployeeCreate,
    EmployeeIdList,
    EmployeeList,
    EmployeeRead,
    EmployeeUpdate,
)
from app.schemas.validators import (
    curp_birth_date_error,
    rfc_birth_date_error,
)
from app.schemas.verification import VerificationLogList, VerificationLogRead
from app.services.availability_service import CURP_TAKEN, NSS_TAKEN, NUMBER_TAKEN, RFC_TAKEN
from app.services.people_service import SHARED_ACCOUNT, PeopleService, ensure_account_free
from app.services.person_erasure import erase_account, erase_employee_biometrics
from app.services.session_service import SessionService
from app.services.trash import commit_restore, ensure_deleted, ensure_live

#: Lo que el sistema guarda como motivo (se traduce al leerse, en el idioma de quien lo lee: `app/i18n/stored.py`).
RESET_BY_COMPANY = stored("ENROLLMENT_RESET_BY_COMPANY")
REVERIFY_DEFAULT_REASON = stored("REVERIFY_DEFAULT_REASON")
# Documentos únicos por persona: (campo, código de error y llave de su mensaje).
_UNIQUE_DOCUMENTS: tuple[tuple[UniqueDocument, str], ...] = (
    ("rfc", RFC_TAKEN),
    ("curp", CURP_TAKEN),
    ("nss", NSS_TAKEN),
)
# Al restaurar: la llave del mensaje cuando otro empleado vigente ya tomó el dato.
_RESTORE_KEYS = {"rfc": "RESTORE_RFC_TAKEN", "curp": "RESTORE_CURP_TAKEN", "nss": "RESTORE_NSS_TAKEN"}
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
        self.departments = DepartmentRepository(db, company_id)
        self.people = PeopleService(db, company_id)

    # ---------- Consultas ----------

    def get(self, employee_id: int, *, include_deleted: bool = False, lock: bool = False) -> Employee:
        """El empleado vigente (404 si no existe, es de otra empresa o está en «Eliminados»); con `include_deleted`,
        también uno eliminado (su detalle con la marca, eliminarlo y restaurarlo)."""
        employee = self.employees.get_by_id(employee_id, lock=lock, include_deleted=include_deleted)
        if employee is None:
            raise NotFoundError(code="EMPLOYEE_NOT_FOUND")
        return employee

    def list_employees(
        self,
        *,
        search: str | None,
        active: bool | None,
        page: PageParams,
        department_id: int | None = None,
        deleted: bool = False,
    ) -> EmployeeList:
        """Los vigentes o, con `deleted`, la papelera (el eliminado más reciente primero)."""
        items, total = self.employees.search(
            search=search,
            active=active,
            offset=page.offset,
            limit=page.size,
            department_id=department_id,
            deleted=deleted,
        )
        samples = self.faces.sample_stats(e.id for e in items)
        shared = self.employees.shared_accounts({e.user_id for e in items})
        departments = self.departments.names(e.department_id for e in items if e.department_id is not None)
        return EmployeeList.of(
            [
                self._to_read(
                    e, samples.get(e.id, NO_SAMPLES), e.user_id in shared, departments.get(e.department_id or 0)
                )
                for e in items
            ],
            total,
            page,
        )

    def ids(self, *, search: str | None, active: bool | None, department_id: int | None) -> EmployeeIdList:
        """Los ids del filtro (hasta el tope de una operación masiva) para seleccionarlos todos."""
        ids, total = self.employees.ids(search=search, active=active, department_id=department_id, limit=BULK_MAX)
        return EmployeeIdList(ids=ids, total=total, limit=BULK_MAX)

    def read(self, employee: Employee) -> EmployeeRead:
        department = self.departments.names((employee.department_id,)) if employee.department_id else {}
        data = self._to_read(
            employee,
            self.faces.sample_stats((employee.id,)).get(employee.id, NO_SAMPLES),
            self.employees.shared_account(employee.user_id),
            department.get(employee.department_id or 0),
        )
        data.managed_departments = [
            DepartmentRef(id=d.id, name=d.name) for d in self.departments.managed_by(employee.id)
        ]
        data.latest_enrollment_id = FaceEnrollmentRepository(self.db, self.company_id).latest_id_for_employee(
            employee.id
        )
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
            raise UnprocessableError(code="PASSWORD_REQUIRED", field="password")

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
            # Historial de altas y bajas: la base del cobro prorrateado por empleado activo.
            BillingRepository(self.db).record_status(self.company_id, employee.id, True)
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError(code="DUPLICATE", key="EMPLOYEE_DUPLICATE") from exc
        self.db.refresh(employee)
        self.db.refresh(employee.user)
        return employee

    def update(self, employee_id: int, data: EmployeeUpdate) -> Employee:
        employee = self.get(employee_id)
        # Un null no cambia nada, salvo en RFC, CURP y NSS (opcionales): ahí null o vacío borra el dato.
        changes = {
            field: value
            for field, value in data.model_dump(exclude_unset=True).items()
            if value is not None or field in OPTIONAL_DOCUMENTS
        }
        # RFC y CURP resultantes contra la fecha resultante (puede cambiar solo uno de los datos).
        self._ensure_documents_match(
            changes.get("rfc", employee.rfc),
            changes.get("curp", employee.curp),
            changes.get("birth_date", employee.birth_date),
        )
        self._ensure_unique(
            employee_number=changes.get("employee_number"),
            documents={field: changes.get(field) for field in OPTIONAL_DOCUMENTS},
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
            raise ConflictError(code="DUPLICATE", key="EMPLOYEE_DUPLICATE") from exc
        self.db.refresh(employee)
        return employee

    def set_active(self, employee_id: int, active: bool) -> Employee:
        """Activa o desactiva el empleo en ESTA empresa (la cuenta y sus otros empleos no cambian).

        Desactivar cierra de inmediato las sesiones en las que la persona entró a esta empresa.
        """
        employee = self.get(employee_id)
        if employee.active != active:  # cuenta para el cobro desde este momento (o deja de contar)
            BillingRepository(self.db).record_status(self.company_id, employee.id, active)
        employee.active = active
        if not active:
            SessionService(self.db).revoke_company(
                employee.user_id, self.company_id, SessionRevocationReason.ACCOUNT_DEACTIVATED
            )
        self.db.commit()
        self.db.refresh(employee)
        return employee

    def delete(self, employee_id: int, actor: User) -> None:
        """Manda el empleo en esta empresa a «Eliminados» (borrado lógico): sale de listados, tablero, galería facial,
        conteos y del cobro desde hoy; su asistencia, bitácora y cobranza se conservan. Sus datos biométricos y fotos
        se borran DE VERDAD (regla 13), sus solicitudes pendientes se cancelan y su QR deja de servir. Sus
        departamentos dejan de mostrarlo como responsable sin borrar la relación (la oculta el borrado lógico): si se
        restaura, vuelve a serlo. Su cuenta también va a «Eliminados» si no tiene otro empleo vigente (su correo y su
        teléfono quedan libres; su foto de perfil se borra). Todo en una transacción."""
        employee = self.get(employee_id, include_deleted=True, lock=True)
        ensure_live(employee)
        user, now = employee.user, datetime.now(UTC)
        # ¿Trabaja en otra empresa? (cruza empresas solo para eso: con la seguridad por fila, `user.employees`
        # solo trae los de ESTA empresa). Su cuenta se queda si tiene otro empleo vigente.
        keep_account = self.employees.shared_account(user.id)
        sessions = SessionService(self.db)
        if keep_account:
            sessions.revoke_company(user.id, self.company_id, SessionRevocationReason.EMPLOYEE_REMOVED)
        else:
            sessions.revoke_all(user.id, SessionRevocationReason.EMPLOYEE_REMOVED, commit=False)
        if employee.active:  # deja de contar para el cobro desde hoy (los días que estuvo activo se cobran igual)
            BillingRepository(self.db).record_status(self.company_id, employee.id, False)
        erase_employee_biometrics(self.db, employee)
        CalendarRepository(self.db, self.company_id).cancel_pending_absences(employee.id, actor.id, now)
        ShiftRepository(self.db, self.company_id).cancel_pending_requests(
            actor_id=actor.id, now=now, employee_id=employee.id
        )
        EmployeeQrRepository(self.db).revoke_all_for_employee(employee.id)
        employee.mark_deleted(now, actor.email)
        if not keep_account:
            erase_account(self.db, user)
            user.mark_deleted(now, actor.email)
        self.db.commit()

    def restore(self, employee_id: int) -> Employee:
        """Regresa al empleado de «Eliminados», revisando de nuevo lo que pudo cambiar mientras tanto: el límite de
        empleados del plan, que nadie vigente haya tomado su número, RFC, CURP o NSS y, si su cuenta también se
        eliminó, su correo y su teléfono (409 `RESTORE_CONFLICT` con el campo). Vuelve a contar para el cobro desde
        hoy si estaba activo y vuelve a ser responsable de los departamentos que dirigía (los que siguen vigentes; uno
        en «Eliminados» lo vuelve a mostrar cuando también se restaure). Sus datos biométricos no regresan: registra su
        rostro de nuevo."""
        employee = self.get(employee_id, include_deleted=True, lock=True)
        ensure_deleted(employee)
        self._ensure_capacity()
        self._ensure_identifiers_free(employee)
        user = employee.user
        if user.deleted:
            ensure_account_free(self.users, user)
            user.mark_restored()
        elif self.employees.has_employment(user.id):
            raise ConflictError(code="RESTORE_CONFLICT", key="RESTORE_EMPLOYMENT_TAKEN", field="email")
        employee.mark_restored()
        if employee.active:
            BillingRepository(self.db).record_status(self.company_id, employee.id, True)
        commit_restore(self.db)
        self.db.refresh(employee)
        return employee

    def reset_face(self, employee_id: int, reason: str | None = None) -> Employee:
        """Solicita al empleado verificar de nuevo su identidad: elimina sus datos faciales y, en
        su próximo acceso, deberá registrar su rostro y COMPANY validarlo otra vez.

        El motivo (o uno genérico) se le muestra al empleado al entrar al registro facial.
        """
        employee = self.get(employee_id)
        self._request_reverification(reason, employee.id)
        self.db.commit()
        self.db.refresh(employee)
        return employee

    def reset_all_faces(self, reason: str | None = None) -> int:
        """Solicita a TODOS los empleados de la empresa con un registro facial (aprobado, en
        validación o rechazado) verificar de nuevo su identidad: p. ej. tras un incidente de
        seguridad, un cambio de cámaras o una auditoría. Mismas reglas que `reset_face`, en unas
        cuantas sentencias para toda la empresa (no una por empleado). Devuelve cuántos cambiaron."""
        changed = self._request_reverification(reason)
        self.db.commit()
        return changed

    # ---------- Internos ----------

    def _ensure_identifiers_free(self, employee: Employee) -> None:
        """Al restaurar: su número y sus documentos no los tiene ya otro empleado vigente de la empresa."""
        if self.employees.number_exists(employee.employee_number):
            raise ConflictError(
                code="RESTORE_CONFLICT",
                key="RESTORE_EMPLOYEE_NUMBER_TAKEN",
                params={"value": employee.employee_number},
                field="employee_number",
            )
        for field, _ in _UNIQUE_DOCUMENTS:
            value = getattr(employee, field)
            if value and self.employees.unique_exists(field, value):
                raise ConflictError(
                    code="RESTORE_CONFLICT", key=_RESTORE_KEYS[field], params={"value": value}, field=field
                )

    def _request_reverification(self, reason: str | None, employee_id: int | None = None) -> int:
        """Rechaza lo que estaba en validación, borra los datos faciales (aprobados y aprendidos) y
        regresa a los empleados a "sin registro facial" con el motivo que verán al entrar."""
        message = (reason or "").strip() or REVERIFY_DEFAULT_REASON
        FaceEnrollmentRepository(self.db, self.company_id).reject_pending(RESET_BY_COMPANY, employee_id)
        self.faces.delete_for_company(self.company_id, employee_id)
        return self.employees.request_reenrollment(message, employee_id)

    def _ensure_account_changes(self, employee: Employee, changes: dict[str, object]) -> None:
        """Correo, teléfono y contraseña son de la cuenta de la persona: únicos en la plataforma y,
        si también trabaja en otra empresa, ninguna empresa puede cambiarlos (solo ella)."""
        user = employee.user
        changed: set[str] = {f for f in ("email", "phone") if f in changes and changes[f] != getattr(user, f)}
        if "password" in changes:
            changed.add("password")
        if changed and self.employees.shared_account(employee.user_id):
            raise ConflictError(code="SHARED_ACCOUNT", key=SHARED_ACCOUNT, field=min(changed))
        checks = {"email": self.people.check_email, "phone": self.people.check_phone}
        for field in sorted(changed & checks.keys()):
            result = checks[field](str(changes[field]), exclude_user_id=user.id)
            if result.match == "TAKEN":
                raise ConflictError(result.message, code=f"{field.upper()}_TAKEN", field=field)

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
            raise ConflictError(code=NUMBER_TAKEN, field="employee_number")
        exclude_id = exclude_employee.id if exclude_employee else None
        for field, code in _UNIQUE_DOCUMENTS:
            value = (documents or {}).get(field)
            if value and self.employees.unique_exists(field, value, exclude_id=exclude_id):
                raise ConflictError(code=code, field=field)

    def _ensure_capacity(self) -> None:
        """Límite de empleados del plan de la empresa (lo define el administrador de la plataforma)."""
        company = self.db.get(Company, self.company_id)
        if (
            company is not None
            and company.max_employees is not None
            and self.employees.count() >= company.max_employees
        ):
            raise ConflictError(code="EMPLOYEE_LIMIT_REACHED", params={"count": company.max_employees})

    @staticmethod
    def _ensure_documents_match(rfc: str | None, curp: str | None, birth_date: date) -> None:
        """RFC y CURP llevan la fecha de nacimiento: si se capturaron, deben coincidir con la registrada."""
        rfc_error = rfc_birth_date_error(rfc, birth_date) if rfc else None
        if rfc_error:
            raise UnprocessableError(str(rfc_error), code="RFC_BIRTH_DATE_MISMATCH", field="rfc")
        curp_error = curp_birth_date_error(curp, birth_date) if curp else None
        if curp_error:
            raise UnprocessableError(str(curp_error), code="CURP_BIRTH_DATE_MISMATCH", field="curp")

    @staticmethod
    def _to_read(
        employee: Employee, samples: SampleStats, shared: bool, department_name: str | None = None
    ) -> EmployeeRead:
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
            shared_account=shared,
            active=employee.active,
            headwear_exempt=employee.headwear_exempt,
            face_status=employee.face_status,
            face_rejection_reason=employee.face_rejection_reason,
            has_face=samples.total > 0,
            face_samples=samples.total,
            department_id=employee.department_id,
            department_name=department_name,
            # La cuenta ya viene con el empleado (JOIN): la foto no cuesta otra consulta.
            avatar=avatar_path(employee.user_id, employee.user.avatar_version) if employee.active else None,
            created_at=employee.created_at,
            updated_at=employee.updated_at,
            **deletion_of(employee),
        )
