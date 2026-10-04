"""Departamentos de UNA empresa (rol COMPANY): alta, edición, responsables y empleados asignados.

- Cada empleado está a lo más en un departamento: asignarlo a otro lo cambia de departamento.
- Responsables: empleados de la misma empresa (pueden ser varios y dirigir varios departamentos).
- Un departamento con empleados no se borra (409): primero se reasignan o se quitan.

Asignar, quitar, nombrar y retirar responsables son idempotentes: repetir la misma petición (p. ej.
un reintento tras perder la conexión) deja el mismo resultado y no responde un error.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.models import Department, Employee
from app.repositories.department_repository import DepartmentRepository
from app.repositories.employee_repository import EmployeeRepository
from app.schemas.common import PageParams
from app.schemas.department import DepartmentCreate, DepartmentList, DepartmentPerson, DepartmentRead, clean_name
from app.services.availability_service import Availability

NAME_TAKEN = "Ya existe un departamento con ese nombre en tu empresa"


class DepartmentService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = DepartmentRepository(db, company_id)
        self.employees = EmployeeRepository(db, company_id)

    # ---------- Consultas ----------

    def list_departments(self, *, search: str | None, page: PageParams) -> DepartmentList:
        items, total = self.repo.search(search=search, offset=page.offset, limit=page.size)
        return DepartmentList.of(self._read_many(items), total, page)

    def get(self, department_id: int) -> Department:
        department = self.repo.get(department_id)
        if department is None:
            raise NotFoundError("Departamento no encontrado", code="DEPARTMENT_NOT_FOUND")
        return department

    def read(self, department: Department) -> DepartmentRead:
        return self._read_many([department])[0]

    def name_availability(self, value: str, exclude_id: int | None = None) -> Availability:
        """Validación en vivo del nombre (mismo contrato que los demás campos únicos)."""
        name = clean_name(value)
        if not name:
            return Availability("department_name", value, None, False, False, "EMPTY", "El nombre es obligatorio")
        if len(name) > 100:
            message = "El nombre admite hasta 100 caracteres"
            return Availability("department_name", value, None, False, False, "INVALID_FORMAT", message)
        if self.repo.name_exists(name, exclude_id):
            return Availability("department_name", value, name, True, False, "TAKEN", NAME_TAKEN)
        return Availability("department_name", value, name, True, True, "AVAILABLE", "Nombre disponible")

    # ---------- Comandos ----------

    def create(self, data: DepartmentCreate) -> Department:
        self._ensure_name_free(data.name)
        department = self.repo.add(Department(name=data.name, description=data.description))
        self.db.commit()
        return department

    def update(self, department_id: int, data: DepartmentCreate) -> Department:
        department = self.get(department_id)
        self._ensure_name_free(data.name, exclude_id=department.id)
        department.name = data.name
        department.description = data.description
        self.db.commit()
        return department

    def delete(self, department_id: int) -> None:
        department = self.get(department_id)
        if self.repo.member_counts((department.id,)).get(department.id):
            raise ConflictError(
                "El departamento tiene empleados asignados: reasígnalos o quítalos antes de eliminarlo",
                code="DEPARTMENT_HAS_EMPLOYEES",
            )
        self.repo.delete(department)
        self.db.commit()

    def assign(self, department_id: int, employee_id: int) -> Department:
        """Asigna el empleado al departamento (si estaba en otro, lo cambia)."""
        department = self.get(department_id)
        self._employee(employee_id).department_id = department.id
        self.db.commit()
        return department

    def unassign(self, department_id: int, employee_id: int) -> Department:
        """Quita al empleado del departamento (si ya no estaba en él, no cambia nada)."""
        department = self.get(department_id)
        employee = self._employee(employee_id)
        if employee.department_id == department.id:
            employee.department_id = None
            self.db.commit()
        return department

    def add_manager(self, department_id: int, employee_id: int) -> Department:
        department = self.get(department_id)
        employee = self._employee(employee_id)
        if not self.repo.is_manager(department.id, employee.id):
            try:
                self.repo.add_manager(department.id, employee.id)
                self.db.commit()
            except IntegrityError:  # un reintento simultáneo ya lo nombró: el resultado es el mismo
                self.db.rollback()
        return department

    def remove_manager(self, department_id: int, employee_id: int) -> Department:
        department = self.get(department_id)
        if self.repo.remove_manager(department.id, employee_id):
            self.db.commit()
        return department

    # ---------- Internos ----------

    def _ensure_name_free(self, name: str, exclude_id: int | None = None) -> None:
        if self.repo.name_exists(name, exclude_id):
            raise ConflictError(NAME_TAKEN, code="DEPARTMENT_NAME_TAKEN", field="name")

    def _employee(self, employee_id: int) -> Employee:
        employee = self.employees.get_by_id(employee_id)
        if employee is None:
            raise NotFoundError("Empleado no encontrado", code="EMPLOYEE_NOT_FOUND")
        return employee

    def _read_many(self, departments: list[Department]) -> list[DepartmentRead]:
        """Departamentos con su conteo y sus responsables: dos consultas para toda la página."""
        ids = [d.id for d in departments]
        counts = self.repo.member_counts(ids)
        managers = self.repo.managers_of(ids)
        return [
            DepartmentRead(
                id=d.id,
                name=d.name,
                description=d.description,
                employee_count=counts.get(d.id, 0),
                managers=[
                    DepartmentPerson(
                        employee_id=e.id, full_name=e.full_name, employee_number=e.employee_number, active=e.active
                    )
                    for e in managers.get(d.id, [])
                ],
                created_at=d.created_at,
                updated_at=d.updated_at,
            )
            for d in departments
        ]
