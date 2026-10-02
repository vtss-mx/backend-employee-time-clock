"""Alta y administración de empresas (tenants) — exclusivo del ADMIN de la plataforma.

El ADMIN no accede a los empleados ni a los datos biométricos de las empresas: solo a sus datos
fiscales y de contacto, sus administradores y conteos.
"""

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.core.passwords import hash_password
from app.models import Company, SessionRevocationReason, User, UserRole
from app.repositories.company_repository import CompanyRepository
from app.repositories.user_repository import UserRepository
from app.schemas.company import (
    CompanyAdminCreate,
    CompanyAdminPasswordReset,
    CompanyAdminRead,
    CompanyCreate,
    CompanyDetail,
    CompanyList,
    CompanyRead,
    CompanyUpdate,
    PlatformStats,
)
from app.schemas.validators import normalize_company_rfc, normalize_email
from app.services.availability_service import Availability
from app.services.policy_service import PolicyService, clear_policy_cache

RFC_TAKEN = "Ya existe una empresa con ese RFC"
EMAIL_TAKEN = "El correo ya está registrado en la plataforma"
LAST_ADMIN = "La empresa debe conservar al menos un administrador activo"
HAS_EMPLOYEES = "La empresa tiene empleados registrados: desactívala en lugar de eliminarla"
_COMPANY_FIELDS = ("name", "legal_name", "rfc", "contact_email", "phone", "max_employees")

AvailabilityField = Literal["rfc", "admin_email"]


class CompanyService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.companies = CompanyRepository(db)
        self.users = UserRepository(db)

    # ---------- Consultas ----------

    def get(self, company_id: int) -> Company:
        company = self.companies.get(company_id)
        if company is None:
            raise NotFoundError("Empresa no encontrada", code="COMPANY_NOT_FOUND")
        return company

    def list_companies(self, *, search: str | None, active: bool | None, page: int, size: int) -> CompanyList:
        items, total = self.companies.search(search=search, active=active, offset=(page - 1) * size, limit=size)
        counts = self.companies.counts(c.id for c in items)
        return CompanyList(
            items=[self._to_read(c, *counts.get(c.id, (0, 0))) for c in items], total=total, page=page, size=size
        )

    def detail(self, company_id: int) -> CompanyDetail:
        company = self.get(company_id)
        employees, admins = self.companies.counts([company.id])[company.id]
        return CompanyDetail(
            **self._to_read(company, employees, admins).model_dump(),
            admins=[CompanyAdminRead.model_validate(u) for u in self.companies.admins(company.id)],
        )

    def stats(self) -> PlatformStats:
        companies, active, employees, admins = self.companies.stats()
        return PlatformStats(companies=companies, active_companies=active, employees=employees, company_admins=admins)

    def availability(self, field: AvailabilityField, value: str, exclude_id: int | None = None) -> Availability:
        """Misma respuesta que la validación en vivo de empleados (el frontend la trata igual)."""
        is_rfc = field == "rfc"
        raw = (value or "").strip()
        if not raw:
            message = "El RFC es obligatorio" if is_rfc else "El correo es obligatorio"
            return Availability(field, value, None, False, False, "EMPTY", message)
        try:
            normalized = normalize_company_rfc(raw) if is_rfc else normalize_email(raw)
        except ValueError as exc:
            return Availability(field, value, None, False, False, "INVALID_FORMAT", str(exc))
        taken = self.companies.rfc_exists(normalized, exclude_id) if is_rfc else self.users.email_exists(normalized)
        if taken:
            return Availability(field, value, normalized, True, False, "TAKEN", RFC_TAKEN if is_rfc else EMAIL_TAKEN)
        message = "RFC disponible" if is_rfc else "Correo disponible"
        return Availability(field, value, normalized, True, True, "AVAILABLE", message)

    # ---------- Comandos ----------

    def create(self, data: CompanyCreate) -> CompanyDetail:
        """Empresa + su política de verificación (valores seguros) + su primer administrador."""
        if self.companies.rfc_exists(data.rfc):
            raise ConflictError(RFC_TAKEN, code="COMPANY_RFC_TAKEN", field="rfc")
        if self.users.email_exists(data.admin_email):
            raise ConflictError(EMAIL_TAKEN, code="EMAIL_TAKEN", field="admin_email")
        try:
            company = self.companies.add(Company(**data.model_dump(include=set(_COMPANY_FIELDS)), active=True))
            PolicyService(self.db, company.id).ensure()
            self.users.create(
                email=data.admin_email,
                password_hash=hash_password(data.admin_password),
                role=UserRole.COMPANY,
                company_id=company.id,
            )
            self.db.commit()
        except IntegrityError as exc:  # carrera con otra alta simultánea
            self.db.rollback()
            raise ConflictError("El RFC o el correo ya están registrados", code="DUPLICATE") from exc
        return self.detail(company.id)

    def update(self, company_id: int, data: CompanyUpdate) -> CompanyDetail:
        company = self.get(company_id)
        changes = data.model_dump(exclude_unset=True)
        if changes.get("rfc") and self.companies.rfc_exists(changes["rfc"], exclude_id=company.id):
            raise ConflictError(RFC_TAKEN, code="COMPANY_RFC_TAKEN", field="rfc")
        for field in _COMPANY_FIELDS:
            # max_employees admite null explícito (quitar el límite); el resto no se borra.
            if field in changes and (changes[field] is not None or field == "max_employees"):
                setattr(company, field, changes[field])
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError(RFC_TAKEN, code="COMPANY_RFC_TAKEN", field="rfc") from exc
        return self.detail(company.id)

    def set_active(self, company_id: int, active: bool) -> CompanyDetail:
        """Desactivar corta el acceso de todo su personal de inmediato (sesiones revocadas)."""
        company = self.get(company_id)
        company.active = active
        if not active:
            self.companies.revoke_sessions(
                datetime.now(UTC), SessionRevocationReason.COMPANY_DEACTIVATED, company_id=company.id
            )
        self.db.commit()
        clear_policy_cache(company.id)
        return self.detail(company.id)

    def delete(self, company_id: int) -> None:
        """Solo una empresa sin empleados (p. ej. registrada por error); con empleados se desactiva."""
        company = self.get(company_id)
        if self.companies.employee_count(company.id) > 0:
            raise ConflictError(HAS_EMPLOYEES, code="COMPANY_HAS_EMPLOYEES")
        self.companies.delete(company)
        self.db.commit()
        clear_policy_cache(company_id)

    def add_admin(self, company_id: int, data: CompanyAdminCreate) -> CompanyDetail:
        company = self.get(company_id)
        if self.users.email_exists(data.admin_email):
            raise ConflictError(EMAIL_TAKEN, code="EMAIL_TAKEN", field="admin_email")
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
            raise ConflictError(LAST_ADMIN, code="LAST_COMPANY_ADMIN")
        admin.active = active
        if not active:
            self.companies.revoke_sessions(
                datetime.now(UTC), SessionRevocationReason.ACCOUNT_DEACTIVATED, user_id=admin.id
            )
        self.db.commit()
        return self.detail(company.id)

    def reset_admin_password(self, company_id: int, user_id: int, data: CompanyAdminPasswordReset) -> CompanyDetail:
        """Contraseña olvidada: se asigna una nueva y se cierran las sesiones abiertas del administrador."""
        company, admin = self._company_admin(company_id, user_id)
        admin.password_hash = hash_password(data.admin_password)
        self.companies.revoke_sessions(datetime.now(UTC), SessionRevocationReason.PASSWORD_RESET, user_id=admin.id)
        self.db.commit()
        return self.detail(company.id)

    # ---------- Internos ----------

    def _company_admin(self, company_id: int, user_id: int) -> tuple[Company, User]:
        """Empresa y uno de SUS administradores (un usuario de otra empresa responde 404)."""
        company = self.get(company_id)
        admin = self.db.get(User, user_id)
        if admin is None or admin.company_id != company.id or admin.role != UserRole.COMPANY:
            raise NotFoundError("Administrador no encontrado", code="COMPANY_ADMIN_NOT_FOUND")
        return company, admin

    @staticmethod
    def _to_read(company: Company, employees: int, admins: int) -> CompanyRead:
        return CompanyRead.model_validate(company).model_copy(
            update={"employee_count": employees, "admin_count": admins}
        )
