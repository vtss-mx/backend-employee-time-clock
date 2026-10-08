"""Departamentos de UNA empresa (rol COMPANY): alta, edición, responsables y empleados asignados.

- Cada empleado está a lo más en un departamento: asignarlo a otro lo cambia de departamento.
- Responsables: empleados de la misma empresa (pueden ser varios y dirigir varios departamentos).
- Un departamento con empleados no se borra (409): primero se reasignan o se quitan.

Asignar, quitar, nombrar y retirar responsables son idempotentes: repetir la misma petición (p. ej.
un reintento tras perder la conexión) deja el mismo resultado y no responde un error.

Eliminar es un borrado lógico (regla 20 de la raíz): va a «Eliminados» y su nombre queda libre; restaurarlo revisa que
siga libre. Sus responsables NO se borran (decisión del dueño, 2026-10-06: restaurar regresa todo como estaba): la
relación se conserva y el borrado lógico la oculta mientras el departamento o el empleado estén en «Eliminados»
(`managers_of` y `managed_by` unen la tabla con borrado lógico del otro lado). Restaurar cualquiera de los dos la
vuelve a mostrar en cuanto ambos están vigentes; la depuración del año la borra en cascada con cualquiera de ellos.
"""

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.i18n import Text
from app.models import Department, Employee, User
from app.repositories.department_repository import DepartmentRepository
from app.repositories.employee_repository import EmployeeRepository
from app.schemas.avatar import employee_avatar
from app.schemas.common import PageParams, deletion_of
from app.schemas.department import DepartmentCreate, DepartmentList, DepartmentPerson, DepartmentRead, clean_name
from app.services.availability_service import Availability
from app.services.trash import commit_restore, ensure_deleted, ensure_live, ensure_name_free


class DepartmentService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = DepartmentRepository(db, company_id)
        self.employees = EmployeeRepository(db, company_id)

    # ---------- Consultas ----------

    def list_departments(self, *, search: str | None, page: PageParams, deleted: bool = False) -> DepartmentList:
        """Los vigentes o, con `deleted`, la papelera (el eliminado más reciente primero)."""
        items, total = self.repo.search(search=search, offset=page.offset, limit=page.size, deleted=deleted)
        return DepartmentList.of(self._read_many(items), total, page)

    def get(self, department_id: int, *, include_deleted: bool = False) -> Department:
        """El vigente (404 si no existe o está en «Eliminados»); con `include_deleted`, también uno eliminado."""
        department = self.repo.get(department_id, include_deleted=include_deleted)
        if department is None:
            raise NotFoundError(code="DEPARTMENT_NOT_FOUND")
        return department

    def read(self, department: Department) -> DepartmentRead:
        return self._read_many([department])[0]

    def name_availability(self, value: str, exclude_id: int | None = None) -> Availability:
        """Validación en vivo del nombre (mismo contrato que los demás campos únicos)."""
        name = clean_name(value)
        if not name:
            return Availability("department_name", value, None, False, False, "EMPTY", Text("NAME_REQUIRED"))
        if len(name) > 100:
            too_long = Text("NAME_TOO_LONG", {"count": 100})
            return Availability("department_name", value, None, False, False, "INVALID_FORMAT", too_long)
        if self.repo.name_exists(name, exclude_id):
            return Availability("department_name", value, name, True, False, "TAKEN", Text("DEPARTMENT_NAME_TAKEN"))
        return Availability("department_name", value, name, True, True, "AVAILABLE", Text("NAME_AVAILABLE"))

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

    def delete(self, department_id: int, actor: User) -> None:
        """A «Eliminados» (solo sin empleados vigentes): sus responsables se conservan ocultos (regresan al
        restaurarlo) y los empleados en «Eliminados» que aún lo tenían quedan sin departamento (así se puede depurar
        después)."""
        department = self.get(department_id, include_deleted=True)
        ensure_live(department)
        if self.repo.member_counts((department.id,)).get(department.id):
            raise ConflictError(
                code="DEPARTMENT_HAS_EMPLOYEES",
            )
        self.employees.detach_deleted_from(department.id)
        department.mark_deleted(datetime.now(UTC), actor.email)
        self.db.commit()

    def restore(self, department_id: int) -> Department:
        """Regresa de «Eliminados» si su nombre sigue libre (409 `RESTORE_CONFLICT`), con los responsables que tenía
        (los que siguen vigentes; uno en «Eliminados» vuelve cuando también se restaure)."""
        department = self.get(department_id, include_deleted=True)
        ensure_deleted(department)
        ensure_name_free(self.repo.name_exists(department.name), department.name)
        department.mark_restored()
        commit_restore(self.db)
        return department

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
            raise ConflictError(code="DEPARTMENT_NAME_TAKEN", field="name")

    def _employee(self, employee_id: int) -> Employee:
        employee = self.employees.get_by_id(employee_id)
        if employee is None:
            raise NotFoundError(code="EMPLOYEE_NOT_FOUND")
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
                        employee_id=e.id,
                        full_name=e.full_name,
                        employee_number=e.employee_number,
                        active=e.active,
                        # La cuenta viene con el empleado (JOIN de su carga): sin consultas de más.
                        avatar=employee_avatar(e),
                    )
                    for e in managers.get(d.id, [])
                ],
                created_at=d.created_at,
                updated_at=d.updated_at,
                **deletion_of(d),
            )
            for d in departments
        ]
