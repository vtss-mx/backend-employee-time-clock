from collections.abc import Iterable
from typing import Literal

from sqlalchemy import ColumnElement, Select, func, select, union, update
from sqlalchemy.orm import Session

from app.models import Employee, FaceStatus, User
from app.models.employee import employee_search_text
from app.repositories.aggregates import affected_rows, paginate

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

    def get_by_id(self, employee_id: int, *, lock: bool = False) -> Employee | None:
        """`lock`: candado de fila hasta terminar la transacción (cambios del mismo empleado en orden)."""
        # FOR UPDATE OF employees: el usuario y la empresa se cargan con LEFT JOIN (no se bloquean).
        locking = {"of": Employee} if lock else None
        employee = self.db.get(Employee, employee_id, with_for_update=locking, populate_existing=lock)
        return employee if employee is not None and employee.company_id == self.company_id else None

    def by_ids(self, employee_ids: set[int]) -> dict[int, Employee]:
        """Varios empleados de la empresa en una consulta (p. ej. para nombrar una bitácora)."""
        if not employee_ids:
            return {}
        rows = self.db.scalars(select(Employee).where(self._scoped(), Employee.id.in_(employee_ids)))
        return {e.id: e for e in rows}

    def shared_accounts(self, user_ids: set[int]) -> set[int]:
        """Cuentas (de esta página) que también trabajan en otra empresa, en UNA consulta.

        Es el único dato que cruza empresas y solo dice "tiene otro empleo" (un conteo, ningún dato
        de la otra empresa).
        """
        if not user_ids:
            return set()
        rows = self.db.execute(
            select(Employee.user_id)
            .where(Employee.user_id.in_(user_ids))
            .group_by(Employee.user_id)
            .having(func.count() > 1)
        )
        return {row.user_id for row in rows}

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

    def lock_many(self, employee_ids: Iterable[int]) -> list[Employee]:
        """Varios empleados de la empresa con candado de fila, en orden de id: dos operaciones masivas
        que comparten empleados los bloquean en el mismo orden (sin interbloqueos). FOR UPDATE OF
        employees: el usuario y la empresa se cargan con LEFT JOIN (no se bloquean)."""
        stmt = (
            select(Employee)
            .where(self._scoped(), Employee.id.in_(set(employee_ids)))
            .order_by(Employee.id)
            .with_for_update(of=Employee)
            .execution_options(populate_existing=True)
        )
        return list(self.db.scalars(stmt))

    def search(
        self, *, search: str | None, active: bool | None, offset: int, limit: int, department_id: int | None = None
    ) -> tuple[list[Employee], int]:
        stmt = self._filtered(select(Employee), search=search, active=active, department_id=department_id)
        order = (Employee.last_name, Employee.first_name, Employee.id)
        return paginate(self.db, stmt, order, offset=offset, limit=limit)

    def ids(
        self, *, search: str | None, active: bool | None, department_id: int | None, limit: int
    ) -> tuple[list[int], int]:
        """Los ids de los empleados que coinciden con los filtros del listado (los primeros `limit`, en
        el orden del listado) y cuántos son: "seleccionar todos los de este filtro"."""
        stmt = self._filtered(select(Employee.id), search=search, active=active, department_id=department_id)
        total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        page = stmt.order_by(Employee.last_name, Employee.first_name, Employee.id).limit(limit)
        return list(self.db.scalars(page)), int(total)

    def _filtered[RowT](
        self, stmt: Select[RowT], *, search: str | None, active: bool | None, department_id: int | None
    ) -> Select[RowT]:
        """Los filtros del listado de empleados (una sola definición para la página y para los ids).

        Sin JOIN con las cuentas: ningún filtro lo necesita (el correo se busca en su propia rama) y con
        él el conteo de la página recorría `users` completa de toda la plataforma en lugar de leer solo
        el índice de la empresa."""
        stmt = stmt.where(self._scoped())
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
        if department_id is not None:  # índice (company_id, department_id, apellidos, nombre, id)
            stmt = stmt.where(Employee.department_id == department_id)
        return stmt

    def add(self, employee: Employee) -> Employee:
        employee.company_id = self.company_id
        self.db.add(employee)
        self.db.flush()
        return employee

    def delete(self, employee: Employee) -> None:
        self.db.delete(employee)

    def request_reenrollment(self, reason: str, employee_id: int | None = None) -> int:
        """Regresa a "sin registro facial" (con el motivo que verán) a un empleado o a todos los que
        tenían un registro, en una sola sentencia. Cuántos cambiaron."""
        stmt = update(Employee).where(self._scoped())
        if employee_id is not None:
            stmt = stmt.where(Employee.id == employee_id)
        else:
            stmt = stmt.where(Employee.face_status != FaceStatus.NOT_ENROLLED)
        return affected_rows(self.db, stmt.values(face_status=FaceStatus.NOT_ENROLLED, face_rejection_reason=reason))
