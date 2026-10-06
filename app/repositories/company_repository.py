from collections.abc import Iterable
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.soft_delete import WITH_DELETED, with_deleted
from app.models import Company, Employee, User, UserRole
from app.models.company import TaxId, company_search_text
from app.repositories.aggregates import group_counts, paginate, trash_page
from app.repositories.search import contains_text, search_term


class CompanyCounts(NamedTuple):
    """Personal de una empresa que ve el ADMIN."""

    employees: int = 0
    admins: int = 0
    #: Validadores activos: los que cuentan contra su límite y en el cobro.
    active_validators: int = 0


class CompanyRepository:
    """Empresas de la plataforma (solo lo usa el ADMIN). Con borrado lógico: solo las vigentes, salvo la papelera,
    eliminar/restaurar (`include_deleted`) y los nombres que resuelven referencias del historial."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, company_id: int, *, lock: bool = False, include_deleted: bool = False) -> Company | None:
        """`lock`: `FOR UPDATE` de la empresa (el mismo candado de la cobranza: altas de validadores, límites)."""
        options = WITH_DELETED if include_deleted else {}
        lock_of = {"of": Company} if lock else None
        return self.db.get(Company, company_id, with_for_update=lock_of, execution_options=options)

    def add(self, company: Company) -> Company:
        self.db.add(company)
        self.db.flush()
        return company

    def names_by_ids(self, company_ids: Iterable[int]) -> dict[int, str]:
        """Nombre de varias empresas en UNA consulta (p. ej. dónde ocurrió cada error): referencias del historial,
        también las que están en «Eliminados»."""
        ids = set(company_ids)
        if not ids:
            return {}
        rows = self.db.execute(with_deleted(select(Company.id, Company.name).where(Company.id.in_(ids))))
        return {int(i): str(n) for i, n in rows}

    def tax_id_exists(self, tax: TaxId, exclude_id: int | None = None) -> bool:
        """¿Otra empresa VIGENTE ya tiene ese identificador fiscal (país, tipo y número)? Por el único parcial
        `uq_companies_tax_id` (las tres igualdades; lo vigente lo agrega el borrado lógico)."""
        stmt = select(Company.id).where(
            Company.tax_country == tax.country, Company.tax_id_type == tax.type, Company.tax_id == tax.number
        )
        if exclude_id is not None:
            stmt = stmt.where(Company.id != exclude_id)
        return self.db.scalar(stmt.limit(1)) is not None

    def search(
        self, *, search: str | None, active: bool | None, offset: int, limit: int, deleted: bool = False
    ) -> tuple[list[Company], int]:
        """Las vigentes por nombre o, con `deleted`, la papelera (la eliminada más reciente primero)."""
        stmt = select(Company)
        term = search_term(search)
        if term:  # índice GIN de trigramas sobre nombre, razón social e identificador fiscal
            stmt = stmt.where(contains_text(company_search_text(), term))
        if active is not None:
            stmt = stmt.where(Company.active.is_(active))
        if deleted:
            return trash_page(self.db, stmt, Company, offset=offset, limit=limit)
        return paginate(self.db, stmt, (func.lower(Company.name), Company.id), offset=offset, limit=limit)

    def counts(self, company_ids: Iterable[int]) -> dict[int, CompanyCounts]:
        """Empleados, administradores y validadores activos por empresa: dos GROUP BY para toda la página, sin N+1
        (las cuentas de administradores y validadores en uno solo, por el índice `(company_id, role)`)."""
        ids = list(company_ids)
        if not ids:
            return {}
        employees = group_counts(
            self.db,
            select(Employee.company_id, func.count()).where(Employee.company_id.in_(ids)).group_by(Employee.company_id),
        )
        rows = self.db.execute(
            select(
                User.company_id,
                func.count().filter(User.role == UserRole.COMPANY),
                func.count().filter(User.role == UserRole.VALIDATOR, User.active.is_(True)),
            )
            .where(User.company_id.in_(ids), User.role.in_((UserRole.COMPANY, UserRole.VALIDATOR)))
            .group_by(User.company_id)
        )
        accounts: dict[int, tuple[int, int]] = {int(row[0] or 0): (int(row[1]), int(row[2])) for row in rows}
        return {cid: CompanyCounts(employees.get(cid, 0), *accounts.get(cid, (0, 0))) for cid in ids}

    def admins_page(self, company_id: int, *, offset: int, limit: int) -> tuple[list[User], int]:
        stmt = select(User).where(User.company_id == company_id, User.role == UserRole.COMPANY)
        return paginate(self.db, stmt, (User.created_at, User.id), offset=offset, limit=limit)

    def active_admin_count(self, company_id: int) -> int:
        stmt = select(func.count()).where(
            User.company_id == company_id, User.role == UserRole.COMPANY, User.active.is_(True)
        )
        return int(self.db.scalar(stmt) or 0)

    def employee_count(self, company_id: int) -> int:
        return int(self.db.scalar(select(func.count()).where(Employee.company_id == company_id)) or 0)

    def stats(self) -> tuple[int, int, int, int]:
        """(empresas, empresas activas, empleados, administradores de empresa) en una consulta."""
        row = self.db.execute(
            select(
                select(func.count()).select_from(Company).scalar_subquery(),
                select(func.count()).select_from(Company).where(Company.active.is_(True)).scalar_subquery(),
                # Personas (una persona puede trabajar en varias empresas).
                select(func.count(func.distinct(Employee.user_id))).scalar_subquery(),
                select(func.count()).select_from(User).where(User.role == UserRole.COMPANY).scalar_subquery(),
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])
