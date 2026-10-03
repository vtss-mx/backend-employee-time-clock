from typing import Literal

from sqlalchemy import ColumnElement, func, select, union
from sqlalchemy.orm import Session

from app.models import Employee, User
from app.models.employee import employee_search_text
from app.repositories.aggregates import paginate

UniqueDocument = Literal["rfc", "curp", "nss"]


class EmployeeRepository:
    """Empleados de UNA empresa: toda consulta filtra por `company_id` (aislamiento multiempresa).

    Un id de otra empresa se comporta como inexistente (404), nunca como "prohibido": así no se
    revela qué ids existen en otras empresas.
    """

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def _scoped(self) -> ColumnElement[bool]:
        return Employee.company_id == self.company_id

    def get_by_id(self, employee_id: int) -> Employee | None:
        employee = self.db.get(Employee, employee_id)
        return employee if employee is not None and employee.company_id == self.company_id else None

    def get_by_user_id(self, user_id: int) -> Employee | None:
        return self.db.scalar(select(Employee).where(self._scoped(), Employee.user_id == user_id))

    def count(self) -> int:
        return int(self.db.scalar(select(func.count()).select_from(Employee).where(self._scoped())) or 0)

    # Número, RFC, CURP y NSS se guardan normalizados: la igualdad directa usa su índice único
    # (company_id, campo).
    def number_exists(self, employee_number: str, exclude_id: int | None = None) -> bool:
        return self._exists(Employee.employee_number == employee_number.upper(), exclude_id)

    def unique_exists(self, field: UniqueDocument, value: str, exclude_id: int | None = None) -> bool:
        """RFC, CURP o NSS ya registrado en otro empleado de la empresa."""
        return self._exists(getattr(Employee, field) == value.upper(), exclude_id)

    def _exists(self, condition: ColumnElement[bool], exclude_id: int | None) -> bool:
        stmt = select(Employee.id).where(self._scoped(), condition)
        if exclude_id is not None:
            stmt = stmt.where(Employee.id != exclude_id)
        return self.db.scalar(stmt.limit(1)) is not None

    def search(self, *, search: str | None, active: bool | None, offset: int, limit: int) -> tuple[list[Employee], int]:
        stmt = select(Employee).join(User, Employee.user_id == User.id).where(self._scoped())
        term = " ".join((search or "").split()).lower()
        if term:
            # Cada rama usa su índice: GIN (company_id + trigramas) para los datos del empleado y
            # trigramas del correo; un OR entre dos tablas obligaría a recorrer ambas.
            # autoescape: "%" y "_" escritos por el usuario se buscan literalmente.
            matches = union(
                select(Employee.id).where(self._scoped(), employee_search_text().contains(term, autoescape=True)),
                select(Employee.id)
                .join(User, Employee.user_id == User.id)
                .where(self._scoped(), User.email.contains(term, autoescape=True)),
            )
            stmt = stmt.where(Employee.id.in_(matches))
        if active is not None:
            stmt = stmt.where(Employee.active.is_(active))

        order = (Employee.last_name, Employee.first_name, Employee.id)
        return paginate(self.db, stmt, order, offset=offset, limit=limit)

    def add(self, employee: Employee) -> Employee:
        employee.company_id = self.company_id
        self.db.add(employee)
        self.db.flush()
        return employee

    def delete(self, employee: Employee) -> None:
        self.db.delete(employee)
