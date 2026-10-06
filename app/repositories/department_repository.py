from collections.abc import Iterable

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.orm import Session

from app.core.soft_delete import with_deleted
from app.models import Department, DepartmentManager, Employee
from app.repositories.aggregates import affected_rows, get_scoped, group_counts, paginate, trash_page
from app.repositories.search import contains_text, search_term


class DepartmentRepository:
    """Departamentos de UNA empresa: un id de otra empresa se comporta como inexistente (404). Con borrado lógico:
    solo los vigentes, salvo la papelera, eliminar/restaurar y los nombres del historial (`names`)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def _scoped(self) -> ColumnElement[bool]:
        return Department.company_id == self.company_id

    def get(self, department_id: int, *, include_deleted: bool = False) -> Department | None:
        return get_scoped(self.db, Department, department_id, self.company_id, include_deleted=include_deleted)

    def name_exists(self, name: str, exclude_id: int | None = None) -> bool:
        """Nombre ya usado en la empresa (sin distinguir mayúsculas: índice único `lower(name)`)."""
        stmt = select(Department.id).where(self._scoped(), func.lower(Department.name) == name.lower())
        if exclude_id is not None:
            stmt = stmt.where(Department.id != exclude_id)
        return self.db.scalar(stmt.limit(1)) is not None

    def search(
        self, *, search: str | None, offset: int, limit: int, deleted: bool = False
    ) -> tuple[list[Department], int]:
        """Los vigentes por nombre o, con `deleted`, la papelera (el eliminado más reciente primero)."""
        stmt = select(Department).where(self._scoped())
        term = search_term(search)
        if term:
            stmt = stmt.where(contains_text(func.lower(Department.name), term))
        if deleted:
            return trash_page(self.db, stmt, Department, offset=offset, limit=limit)
        return paginate(self.db, stmt, (func.lower(Department.name), Department.id), offset=offset, limit=limit)

    def add(self, department: Department) -> Department:
        department.company_id = self.company_id
        self.db.add(department)
        self.db.flush()
        return department

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
        """Responsables por departamento (orden alfabético): una consulta para toda la página. Un responsable en
        «Eliminados» no aparece (el JOIN con `employees` lleva su condición de vigente) y su relación se conserva:
        vuelve al restaurarlo."""
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
        """Nombre de cada departamento (para mostrarlo junto a los empleados): referencias, también de los que están
        en «Eliminados»."""
        ids = {i for i in department_ids if i is not None}
        if not ids:
            return {}
        stmt = select(Department.id, Department.name).where(self._scoped(), Department.id.in_(ids))
        return {int(department_id): str(name) for department_id, name in self.db.execute(with_deleted(stmt))}

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
        """Departamentos vigentes que dirige un empleado (su expediente; uno en «Eliminados» no aparece hasta que se
        restaure)."""
        return list(
            self.db.scalars(
                select(Department)
                .join(DepartmentManager, DepartmentManager.department_id == Department.id)
                .where(self._scoped(), DepartmentManager.employee_id == employee_id)
                .order_by(func.lower(Department.name), Department.id)
            )
        )
