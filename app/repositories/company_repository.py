from collections.abc import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Company, Employee, User, UserRole
from app.models.company import company_search_text
from app.repositories.aggregates import group_counts, paginate


class CompanyRepository:
    """Empresas de la plataforma (solo lo usa el ADMIN)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, company_id: int) -> Company | None:
        return self.db.get(Company, company_id)

    def add(self, company: Company) -> Company:
        self.db.add(company)
        self.db.flush()
        return company

    def names_by_ids(self, company_ids: Iterable[int]) -> dict[int, str]:
        """Nombre de varias empresas en UNA consulta (p. ej. dónde ocurrió cada error)."""
        ids = set(company_ids)
        if not ids:
            return {}
        return {int(i): str(n) for i, n in self.db.execute(select(Company.id, Company.name).where(Company.id.in_(ids)))}

    def rfc_exists(self, rfc: str, exclude_id: int | None = None) -> bool:
        stmt = select(Company.id).where(Company.rfc == rfc.upper())
        if exclude_id is not None:
            stmt = stmt.where(Company.id != exclude_id)
        return self.db.scalar(stmt.limit(1)) is not None

    def search(self, *, search: str | None, active: bool | None, offset: int, limit: int) -> tuple[list[Company], int]:
        stmt = select(Company)
        term = " ".join((search or "").split()).lower()
        if term:  # índice GIN de trigramas sobre nombre, razón social y RFC
            stmt = stmt.where(company_search_text().contains(term, autoescape=True))
        if active is not None:
            stmt = stmt.where(Company.active.is_(active))
        return paginate(self.db, stmt, (func.lower(Company.name), Company.id), offset=offset, limit=limit)

    def counts(self, company_ids: Iterable[int]) -> dict[int, tuple[int, int]]:
        """(empleados, administradores) por empresa: dos GROUP BY para toda la página, sin N+1."""
        ids = list(company_ids)
        if not ids:
            return {}
        employees = group_counts(
            self.db,
            select(Employee.company_id, func.count()).where(Employee.company_id.in_(ids)).group_by(Employee.company_id),
        )
        admins = group_counts(
            self.db,
            select(User.company_id, func.count())
            .where(User.company_id.in_(ids), User.role == UserRole.COMPANY)
            .group_by(User.company_id),
        )
        return {cid: (employees.get(cid, 0), admins.get(cid, 0)) for cid in ids}

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

    def delete(self, company: Company) -> None:
        """Borra una empresa SIN empleados: primero sus cuentas (administradores y validadores; con
        ellas se van sus sesiones y la configuración de cada validador) y después la empresa (con
        ella, su política y su bitácora)."""
        for stmt in (
            delete(User).where(User.company_id == company.id),
            delete(Company).where(Company.id == company.id),
        ):
            self.db.execute(stmt.execution_options(synchronize_session=False))
        self.db.expunge(company)

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
