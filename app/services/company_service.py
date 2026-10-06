"""Alta y administración de empresas (tenants) — exclusivo del ADMIN de la plataforma.

El ADMIN ve los datos fiscales y de contacto de cada empresa, sus administradores y conteos; de sus
empleados, solo la ficha de trabajo (nunca datos fiscales ni biométricos). También administra el
aprendizaje del reconocimiento facial de cada empresa (su evolución y olvidar lo aprendido de un
empleado): la empresa no lo ve ni lo configura.

Eliminar una empresa es un borrado lógico (regla 20 de la raíz): va a «Eliminados» con sus cuentas (administradores y
validadores, con la MISMA marca de tiempo) y su configuración; restaurarla regresa exactamente lo que se fue con ella.
Las fotos de perfil de sus cuentas y la evidencia de sus casos de fraude se borran de verdad (`person_erasure`).
"""

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.core.passwords import hash_password
from app.i18n import t
from app.models import Company, Employee, SessionRevocationReason, User, UserRole
from app.models.company import TaxId
from app.repositories.billing_repository import BillingRepository
from app.repositories.company_repository import CompanyCounts, CompanyRepository
from app.repositories.department_repository import DepartmentRepository
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.face_repository import NO_SAMPLES, FaceEmbeddingRepository
from app.repositories.user_repository import UserRepository
from app.repositories.validator_repository import ValidatorRepository
from app.schemas.common import PageParams
from app.schemas.company import (
    CompanyAdminCreate,
    CompanyAdminList,
    CompanyAdminPasswordReset,
    CompanyAdminRead,
    CompanyCreate,
    CompanyDetail,
    CompanyEmployeeList,
    CompanyEmployeeRead,
    CompanyList,
    CompanyRead,
    CompanyUpdate,
    PlatformStats,
)
from app.schemas.face import FaceLearningSummary
from app.schemas.tax_ids import is_blank_tax_id, resolve_tax_id, tax_id_name
from app.schemas.validators import normalize_email
from app.services.availability_service import Availability, not_captured
from app.services.billing_ledger import Ledger
from app.services.billing_service import BillingService
from app.services.face_learning import learning_summary
from app.services.people_service import ensure_account_free
from app.services.person_erasure import erase_company_accounts
from app.services.policy_service import PolicyService, clear_policy_cache
from app.services.session_service import SessionService
from app.services.trash import commit_restore, ensure_deleted, ensure_live

_COMPANY_FIELDS = ("name", "legal_name", "phone", "max_employees", "max_validators", "api_enabled")
#: Lo que un null explícito BORRA al editar: el límite de empleados (sin límite). En los demás datos un null no cambia
#: nada. El identificador fiscal (opcional) va aparte: país, tipo y número juntos (`Company.set_tax`).
_CLEARABLE_FIELDS = ("max_employees",)


class CompanyService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.companies = CompanyRepository(db)
        self.users = UserRepository(db)

    # ---------- Consultas ----------

    def get(self, company_id: int, *, include_deleted: bool = False, lock: bool = False) -> Company:
        """La empresa vigente (404 si no existe o está en «Eliminados»); con `include_deleted`, también una eliminada
        (su detalle con la marca, eliminarla y restaurarla)."""
        company = self.companies.get(company_id, lock=lock, include_deleted=include_deleted)
        if company is None:
            raise NotFoundError(code="COMPANY_NOT_FOUND")
        return company

    def list_companies(
        self, *, search: str | None, active: bool | None, page: PageParams, deleted: bool = False
    ) -> CompanyList:
        """Las vigentes o, con `deleted`, la papelera (la eliminada más reciente primero)."""
        items, total = self.companies.search(
            search=search, active=active, offset=page.offset, limit=page.size, deleted=deleted
        )
        counts = self.companies.counts(c.id for c in items)
        return CompanyList.of([self._to_read(c, counts.get(c.id, CompanyCounts())) for c in items], total, page)

    def list_admins(self, company_id: int, page: PageParams) -> CompanyAdminList:
        """Administradores de la empresa, paginados (el más antiguo primero)."""
        self.get(company_id)
        admins, total = self.companies.admins_page(company_id, offset=page.offset, limit=page.size)
        return CompanyAdminList.of([CompanyAdminRead.model_validate(u) for u in admins], total, page)

    def detail(self, company_id: int, *, include_deleted: bool = False) -> CompanyDetail:
        company = self.get(company_id, include_deleted=include_deleted)
        return CompanyDetail(**self._to_read(company, self.companies.counts([company.id])[company.id]).model_dump())

    def admin(self, company_id: int, user_id: int) -> CompanyAdminRead:
        """Un administrador de la empresa (p. ej. para restablecer su contraseña)."""
        return CompanyAdminRead.model_validate(self._company_admin(company_id, user_id)[1])

    def stats(self) -> PlatformStats:
        companies, active, employees, admins = self.companies.stats()
        return PlatformStats(companies=companies, active_companies=active, employees=employees, company_admins=admins)

    def email_availability(self, field: str, value: str) -> Availability:
        """Correo de una cuenta nueva (administrador o validador): único en la plataforma. Misma respuesta que la
        validación en vivo de empleados (el frontend la trata igual)."""
        raw = (value or "").strip()
        if not raw:
            return Availability(field, value, None, False, False, "EMPTY", t("EMAIL_REQUIRED"))
        try:
            normalized = normalize_email(raw)
        except ValueError as exc:
            return Availability(field, value, None, False, False, "INVALID_FORMAT", str(exc))
        if self.users.email_exists(normalized):
            return Availability(field, value, normalized, True, False, "TAKEN", t("EMAIL_TAKEN"))
        return Availability(field, value, normalized, True, True, "AVAILABLE", t("EMAIL_AVAILABLE"))

    def tax_id_availability(
        self, field: str, value: str, country: str | None, type_code: str | None, exclude_id: int | None = None
    ) -> Availability:
        """Identificador fiscal mientras se escribe, con la misma regla que al guardar (`resolve_tax_id`): opcional
        (vacío es válido y no se consulta nada, como los documentos del empleado) y único entre las empresas vigentes
        por país, tipo y número. `exclude_id`: la empresa que se edita."""
        if is_blank_tax_id(value):
            return not_captured(field, value)
        try:
            tax = resolve_tax_id(country, type_code, value)
        except ValueError as exc:
            return Availability(field, value, None, False, False, "INVALID_FORMAT", str(exc))
        if self.companies.tax_id_exists(tax, exclude_id):
            return Availability(field, value, tax.number, True, False, "TAKEN", t("COMPANY_TAX_ID_TAKEN"))
        return Availability(field, value, tax.number, True, True, "AVAILABLE", t("TAX_ID_AVAILABLE"))

    # ---------- Comandos ----------

    def create(self, data: CompanyCreate, actor: str | None = None) -> CompanyDetail:
        """Empresa + su política de verificación (valores seguros) + su primer administrador + su plan de
        cobro, todo en una transacción (o nada). Sin identificador fiscal (opcional) no hay nada que comparar."""
        tax = data.tax()
        self._ensure_tax_id_free(tax)
        if self.users.email_exists(data.admin_email):
            raise ConflictError(code="EMAIL_TAKEN", field="admin_email")
        try:
            company = Company(**data.model_dump(include=set(_COMPANY_FIELDS)), active=True)
            company.set_tax(tax)
            self.companies.add(company)
            PolicyService(self.db, company.id).ensure()
            self.users.create(
                email=data.admin_email,
                password_hash=hash_password(data.admin_password),
                role=UserRole.COMPANY,
                company_id=company.id,
            )
            if data.billing is not None:
                BillingService(self.db).save_plan(company.id, data.billing, actor, commit=False, prefix="billing.")
            self.db.commit()
        except IntegrityError as exc:  # carrera con otra alta simultánea
            self.db.rollback()
            raise ConflictError(code="DUPLICATE", key="COMPANY_DUPLICATE") from exc
        return self.detail(company.id)

    def employees(
        self, company_id: int, *, search: str | None, active: bool | None, page: PageParams
    ) -> CompanyEmployeeList:
        """Empleados de una empresa para el ADMIN (paginados, con búsqueda): solo su ficha de trabajo."""
        self.get(company_id)  # 404 si la empresa no existe
        items, total = EmployeeRepository(self.db, company_id).search(
            search=search, active=active, offset=page.offset, limit=page.size
        )
        return CompanyEmployeeList.of(self._employee_reads(company_id, items), total, page)

    def face_learning(self, company_id: int) -> FaceLearningSummary:
        """Evolución del reconocimiento facial de la empresa (solo el ADMIN la ve)."""
        self.get(company_id)
        return learning_summary(self.db, company_id)

    def forget_learned_face(self, company_id: int, employee_id: int) -> tuple[CompanyEmployeeRead, int]:
        """Olvida lo que el reconocimiento aprendió del uso de un empleado: vuelve a compararse solo con
        su registro aprobado, que no se toca (p. ej. si se duda de alguna identificación). Devuelve su
        ficha y cuántas muestras se olvidaron."""
        self.get(company_id)
        employee = EmployeeRepository(self.db, company_id).get_by_id(employee_id)
        if employee is None:
            raise NotFoundError(code="EMPLOYEE_NOT_FOUND")
        removed = FaceEmbeddingRepository(self.db).delete_learned(employee.id)
        self.db.commit()
        return self._employee_reads(company_id, [employee])[0], removed

    def update(self, company_id: int, data: CompanyUpdate) -> CompanyDetail:
        changes = data.model_dump(exclude_unset=True)
        limit = changes.get("max_validators")
        # El límite de validadores se cambia con la empresa bloqueada (el mismo candado con que la empresa da de
        # alta o activa un validador): nunca queda por debajo de los activos aunque ocurran a la vez.
        company = self.get(company_id) if limit is None else Ledger(self.db).lock(company_id)
        if limit is not None:
            self._ensure_validator_limit(company, limit)
        if "tax_id" in data.model_fields_set:  # el país o el tipo sin el número no cambian nada
            tax = data.tax()
            self._ensure_tax_id_free(tax, exclude_id=company.id)
            company.set_tax(tax)
        for field in _COMPANY_FIELDS:
            if field in changes and (changes[field] is not None or field in _CLEARABLE_FIELDS):
                setattr(company, field, changes[field])
        try:
            self.db.commit()
        except IntegrityError as exc:  # otra empresa tomó el identificador a la vez (el índice único lo detuvo)
            self.db.rollback()
            raise ConflictError(code="COMPANY_TAX_ID_TAKEN", field="tax_id") from exc
        return self.detail(company.id)

    def set_active(self, company_id: int, active: bool) -> CompanyDetail:
        """Desactivar corta el acceso de todo su personal de inmediato (sesiones revocadas)."""
        company = self.get(company_id)
        company.active = active
        if not active:
            SessionService(self.db).close_company(company.id, SessionRevocationReason.COMPANY_DEACTIVATED)
        self.db.commit()
        clear_policy_cache(company.id)
        return self.detail(company.id)

    def delete(self, company_id: int, actor: User) -> None:
        """Solo una empresa sin empleados vigentes ni movimientos de cobranza (p. ej. registrada por error); con
        empleados se desactiva. Va a «Eliminados» con sus cuentas y sus validadores (misma marca de tiempo): sus
        sesiones se cierran, sus validadores dejan de cobrarse desde hoy, sus correos y su identificador fiscal quedan
        libres. Las fotos de perfil de sus cuentas y la evidencia de sus casos se borran de verdad."""
        company = self.get(company_id, include_deleted=True, lock=True)
        ensure_live(company)
        if self.companies.employee_count(company.id) > 0:
            raise ConflictError(code="COMPANY_HAS_EMPLOYEES")
        if BillingRepository(self.db).has_movements(company.id):  # el historial financiero no se borra
            raise ConflictError(code="COMPANY_HAS_BILLING")
        now, validators = datetime.now(UTC), ValidatorRepository(self.db, company.id)
        billing = BillingRepository(self.db)
        for validator_id in validators.active_ids():  # dejan de contar para el cobro desde hoy
            billing.record_validator_status(company.id, validator_id, False)
        SessionService(self.db).close_company(company.id, SessionRevocationReason.ACCOUNT_DELETED)
        erase_company_accounts(self.db, company.id)
        validators.delete_all(now, actor.email)
        self.users.delete_company_accounts(company.id, now, actor.email)
        company.mark_deleted(now, actor.email)
        self.db.commit()
        clear_policy_cache(company_id)

    def restore(self, company_id: int) -> CompanyDetail:
        """Regresa a la empresa de «Eliminados» con las cuentas y los validadores que se eliminaron con ella (las que
        se eliminaron antes, una por una, se quedan allá). Revisa de nuevo que nadie vigente haya tomado su
        identificador fiscal (si lo tiene: es opcional) ni el correo o el teléfono de sus cuentas (409
        `RESTORE_CONFLICT`); sus validadores activos vuelven a cobrarse desde hoy (su límite no pudo cambiar mientras
        estaba eliminada). Las fotos de perfil no regresan."""
        company = self.get(company_id, include_deleted=True, lock=True)
        deleted_at = ensure_deleted(company)
        tax = company.tax
        if tax is not None and self.companies.tax_id_exists(tax):
            raise ConflictError(
                code="RESTORE_CONFLICT",
                key="RESTORE_COMPANY_TAX_ID_TAKEN",
                params={"name": tax_id_name(tax.type), "value": tax.number},
                field="tax_id",
            )
        for account in self.users.deleted_with(company.id, deleted_at):
            ensure_account_free(self.users, account)
        self.users.restore_company_accounts(company.id, deleted_at)
        validators = ValidatorRepository(self.db, company.id)
        validators.restore_all(deleted_at)
        company.mark_restored()
        billing = BillingRepository(self.db)
        for validator_id in validators.active_ids():
            billing.record_validator_status(company.id, validator_id, True)
        commit_restore(self.db)
        clear_policy_cache(company_id)
        return self.detail(company.id)

    def add_admin(self, company_id: int, data: CompanyAdminCreate) -> CompanyDetail:
        company = self.get(company_id)
        if self.users.email_exists(data.admin_email):
            raise ConflictError(code="EMAIL_TAKEN", field="admin_email")
        self.users.create(
            email=data.admin_email,
            password_hash=hash_password(data.admin_password),
            role=UserRole.COMPANY,
            company_id=company.id,
        )
        self.db.commit()
        return self.detail(company.id)

    def set_admin_active(self, company_id: int, user_id: int, active: bool) -> CompanyDetail:
        company, admin = self._company_admin(company_id, user_id)
        if not active and admin.active and self.companies.active_admin_count(company.id) <= 1:
            raise ConflictError(code="LAST_COMPANY_ADMIN")
        admin.active = active
        if not active:
            SessionService(self.db).revoke_all(admin.id, SessionRevocationReason.ACCOUNT_DEACTIVATED, commit=False)
        self.db.commit()
        return self.detail(company.id)

    def reset_admin_password(self, company_id: int, user_id: int, data: CompanyAdminPasswordReset) -> CompanyDetail:
        """Contraseña olvidada: se asigna una nueva y se cierran las sesiones abiertas del administrador."""
        company, admin = self._company_admin(company_id, user_id)
        admin.password_hash = hash_password(data.admin_password)
        SessionService(self.db).revoke_all(admin.id, SessionRevocationReason.PASSWORD_RESET, commit=False)
        self.db.commit()
        return self.detail(company.id)

    # ---------- Internos ----------

    def _ensure_tax_id_free(self, tax: TaxId | None, exclude_id: int | None = None) -> None:
        """409 `COMPANY_TAX_ID_TAKEN` (campo `tax_id`) si otra empresa vigente ya tiene ese identificador fiscal. Sin
        identificador no hay nada que comparar (es opcional)."""
        if tax is not None and self.companies.tax_id_exists(tax, exclude_id):
            raise ConflictError(code="COMPANY_TAX_ID_TAKEN", field="tax_id")

    def _ensure_validator_limit(self, company: Company, limit: int) -> None:
        """El límite de validadores nunca baja de los que la empresa tiene activos (decisión documentada en el README,
        "Validadores por empresa"): un validador nunca se queda sin servicio a mitad de su turno. Para bajarlo (o
        apagar el módulo con 0), la empresa desactiva primero los que sobran."""
        active = ValidatorRepository(self.db, company.id).active_count()
        if limit < active:
            raise ConflictError(
                code="VALIDATOR_LIMIT_BELOW_ACTIVE",
                params={"count": active},
                details={"active": active},
                field="max_validators",
            )

    def _employee_reads(self, company_id: int, employees: list[Employee]) -> list[CompanyEmployeeRead]:
        """Fichas de trabajo con su departamento y lo aprendido: dos consultas para toda la página."""
        departments = DepartmentRepository(self.db, company_id).names(
            e.department_id for e in employees if e.department_id
        )
        samples = FaceEmbeddingRepository(self.db).sample_stats(e.id for e in employees)
        reads = []
        for e in employees:
            learned = samples.get(e.id, NO_SAMPLES)
            reads.append(
                CompanyEmployeeRead(
                    id=e.id,
                    employee_number=e.employee_number,
                    first_name=e.first_name,
                    last_name=e.last_name,
                    department_name=departments.get(e.department_id or 0),
                    email=e.user.email,
                    phone=e.user.phone,
                    active=e.active,
                    face_status=e.face_status,
                    face_learned_samples=learned.learned,
                    face_last_learned_at=learned.last_learned_at,
                )
            )
        return reads

    def _company_admin(self, company_id: int, user_id: int) -> tuple[Company, User]:
        """Empresa y uno de SUS administradores (un usuario de otra empresa responde 404)."""
        company = self.get(company_id)
        admin = self.db.get(User, user_id)
        if admin is None or admin.company_id != company.id or admin.role != UserRole.COMPANY:
            raise NotFoundError(code="COMPANY_ADMIN_NOT_FOUND")
        return company, admin

    @staticmethod
    def _to_read(company: Company, counts: CompanyCounts) -> CompanyRead:
        return CompanyRead.model_validate(company).model_copy(
            update={
                "employee_count": counts.employees,
                "admin_count": counts.admins,
                "active_validators": counts.active_validators,
            }
        )
