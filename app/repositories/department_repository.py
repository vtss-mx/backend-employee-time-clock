from collections.abc import Iterable

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.orm import Session

from app.models import Department, DepartmentManager, Employee
from app.repositories.aggregates import affected_rows, group_counts, paginate


class DepartmentRepository:
    """Departamentos de UNA empresa: un id de otra empresa se comporta como inexistente (404)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def _scoped(self) -> ColumnElement[bool]:
        return Department.company_id == self.company_id

    def get(self, department_id: int) -> Department | None:
        department = self.db.get(Department, department_id)
        return department if department is not None and department.company_id == self.company_id else None

    def name_exists(self, name: str, exclude_id: int | None = None) -> bool:
        """Nombre ya usado en la empresa (sin distinguir mayúsculas: índice único `lower(name)`)."""
        stmt = select(Department.id).where(self._scoped(), func.lower(Department.name) == name.lower())
        if exclude_id is not None:
            stmt = stmt.where(Department.id != exclude_id)
        return self.db.scalar(stmt.limit(1)) is not None

    def search(self, *, search: str | None, offset: int, limit: int) -> tuple[list[Department], int]:
        stmt = select(Department).where(self._scoped())
        term = " ".join((search or "").split()).lower()
        if term:
            stmt = stmt.where(func.lower(Department.name).contains(term, autoescape=True))
        return paginate(self.db, stmt, (func.lower(Department.name), Department.id), offset=offset, limit=limit)

    def add(self, department: Department) -> Department:
        department.company_id = self.company_id
        self.db.add(department)
        self.db.flush()
        return department

    def delete(self, department: Department) -> None:
        """Sus responsables se van con él (ON DELETE CASCADE). Quien llama verifica que no tenga empleados."""
        affected_rows(self.db, delete(Department).where(self._scoped(), Department.id == department.id))
        self.db.expunge(department)

    # ---------- Empleados y responsables ----------

    def member_counts(self, department_ids: Iterable[int]) -> dict[int, int]:
        """Empleados asignados por departamento: un GROUP BY para toda la página."""
        ids = list(department_ids)
        if not ids:
            return {}
        return group_counts(
            self.db,
            select(Employee.department_id, func.count())
            .where(Employee.company_id == self.company_id, Employee.department_id.in_(ids))
            .group_by(Employee.department_id),
        )

    def managers_of(self, department_ids: Iterable[int]) -> dict[int, list[Employee]]:
        """Responsables por departamento (orden alfabético): una consulta para toda la página."""
        ids = list(department_ids)
        if not ids:
            return {}
        rows = self.db.execute(
            select(DepartmentManager.department_id, Employee)
            .join(Employee, DepartmentManager.employee_id == Employee.id)
            .where(DepartmentManager.company_id == self.company_id, DepartmentManager.department_id.in_(ids))
            .order_by(Employee.last_name, Employee.first_name, Employee.id)
        ).all()
        managers: dict[int, list[Employee]] = {department_id: [] for department_id in ids}
        for department_id, employee in rows:
            managers[department_id].append(employee)
        return managers

    def names(self, department_ids: Iterable[int]) -> dict[int, str]:
        """Nombre de cada departamento (para mostrarlo junto a los empleados)."""
        ids = {i for i in department_ids if i is not None}
        if not ids:
            return {}
        rows = self.db.execute(select(Department.id, Department.name).where(self._scoped(), Department.id.in_(ids)))
        return {int(department_id): str(name) for department_id, name in rows}

    def is_manager(self, department_id: int, employee_id: int) -> bool:
        return self.db.get(DepartmentManager, (department_id, employee_id)) is not None

    def add_manager(self, department_id: int, employee_id: int) -> None:
        self.db.add(DepartmentManager(department_id=department_id, employee_id=employee_id, company_id=self.company_id))
        self.db.flush()

    def remove_manager(self, department_id: int, employee_id: int) -> int:
        stmt = delete(DepartmentManager).where(
            DepartmentManager.company_id == self.company_id,
            DepartmentManager.department_id == department_id,
            DepartmentManager.employee_id == employee_id,
        )
        return affected_rows(self.db, stmt)

    def managed_by(self, employee_id: int) -> list[Department]:
        """Departamentos que dirige un empleado (su expediente)."""
        return list(
            self.db.scalars(
                select(Department)
                .join(DepartmentManager, DepartmentManager.department_id == Department.id)
                .where(self._scoped(), DepartmentManager.employee_id == employee_id)
                .order_by(func.lower(Department.name), Department.id)
            )
        )
