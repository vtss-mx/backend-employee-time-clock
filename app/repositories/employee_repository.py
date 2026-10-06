from collections.abc import Iterable
from typing import Literal

from sqlalchemy import ColumnElement, Select, func, select, union, update
from sqlalchemy.orm import Session

from app.core.row_security import crossing_tenants
from app.core.soft_delete import with_deleted
from app.models import Employee, FaceStatus, User
from app.models.employee import employee_search_text
from app.repositories.aggregates import affected_rows, get_scoped, paginate, trash_page
from app.repositories.search import contains_text, search_term

UniqueDocument = Literal["rfc", "curp", "nss"]


class EmployeeRepository:
    """Empleados de UNA empresa: toda consulta filtra por `company_id` (aislamiento multiempresa).

    Un id de otra empresa se comporta como inexistente (404), nunca como "prohibido": así no se
    revela qué ids existen en otras empresas. Con borrado lógico (`app/core/soft_delete.py`): toda consulta ve solo
    a los vigentes, salvo la papelera, eliminar/restaurar (`include_deleted`) y `by_ids` (referencias del historial).
    """

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def _scoped(self) -> ColumnElement[bool]:
        return Employee.company_id == self.company_id

    def get_by_id(self, employee_id: int, *, lock: bool = False, include_deleted: bool = False) -> Employee | None:
        """`lock`: candado de fila hasta terminar la transacción (cambios del mismo empleado en orden).
        `include_deleted`: también uno en «Eliminados» (su detalle, eliminarlo y restaurarlo)."""
        # FOR UPDATE OF employees: el usuario y la empresa se cargan con LEFT JOIN (no se bloquean).
        return get_scoped(self.db, Employee, employee_id, self.company_id, lock=lock, include_deleted=include_deleted)

    def by_ids(self, employee_ids: set[int]) -> dict[int, Employee]:
        """Varios empleados de la empresa en una consulta (p. ej. para nombrar una bitácora). Resuelve referencias
        del historial: también los que están en «Eliminados» (se muestran con su marca)."""
        if not employee_ids:
            return {}
        rows = self.db.scalars(with_deleted(select(Employee).where(self._scoped(), Employee.id.in_(employee_ids))))
        return {e.id: e for e in rows}

    def shared_accounts(self, user_ids: set[int]) -> set[int]:
        """Cuentas (de esta página) que también trabajan en otra empresa, en UNA consulta.

        Es el único dato que cruza empresas en una petición de empresa y solo dice "tiene otro empleo" (un
        conteo, ningún dato de la otra empresa): por eso cruza la seguridad por fila de forma EXPLÍCITA
        (`crossing_tenants`, solo para esta sentencia). Con ella la empresa no puede cambiar correo, teléfono ni
        contraseña de quien también trabaja en otra, y al eliminar el empleo se conserva su cuenta.
        """
        if not user_ids:
            return set()
        stmt = select(Employee.user_id).where(Employee.user_id.in_(user_ids)).group_by(Employee.user_id)
        with crossing_tenants(self.db):
            rows = self.db.execute(stmt.having(func.count() > 1)).all()
        return {row.user_id for row in rows}

    def shared_account(self, user_id: int) -> bool:
        """¿La persona también trabaja en otra empresa? (ver `shared_accounts`)."""
        return user_id in self.shared_accounts({user_id})

    def has_employment(self, user_id: int) -> bool:
        """¿La persona ya tiene un empleo VIGENTE en esta empresa? (índice único parcial `uq_employees_company_user`):
        restaurar el anterior la duplicaría."""
        stmt = select(Employee.id).where(self._scoped(), Employee.user_id == user_id)
        return self.db.scalar(stmt.limit(1)) is not None

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
        self,
        *,
        search: str | None,
        active: bool | None,
        offset: int,
        limit: int,
        department_id: int | None = None,
        deleted: bool = False,
    ) -> tuple[list[Employee], int]:
        """Una página de los vigentes (en orden alfabético) o, con `deleted`, de la papelera (el eliminado más
        reciente primero; índice `ix_employees_deleted`)."""
        stmt = self._filtered(select(Employee), search=search, active=active, department_id=department_id)
        if deleted:
            return trash_page(self.db, stmt, Employee, offset=offset, limit=limit)
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
        term = search_term(search)
        if term:
            # Cada rama usa su índice: GIN (company_id + trigramas) para los datos del empleado y
            # trigramas del correo; un OR entre dos tablas obligaría a recorrer ambas.
            matches = union(
                select(Employee.id).where(self._scoped(), contains_text(employee_search_text(), term)),
                select(Employee.id)
                .join(User, Employee.user_id == User.id)
                .where(self._scoped(), contains_text(User.email, term)),
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

    def detach_deleted_from(self, department_id: int) -> None:
        """Los empleados en «Eliminados» que seguían en un departamento que se elimina quedan sin él (una sentencia):
        así ese departamento se puede depurar después (su FK es RESTRICT) y restaurar a uno nunca lo regresa a un
        departamento eliminado. Los vigentes impiden eliminarlo (409 `DEPARTMENT_HAS_EMPLOYEES`)."""
        stmt = update(Employee).where(
            self._scoped(), Employee.department_id == department_id, Employee.deleted_at.is_not(None)
        )
        affected_rows(self.db, with_deleted(stmt.values(department_id=None)))

    def request_reenrollment(self, reason: str, employee_id: int | None = None) -> int:
        """Regresa a "sin registro facial" (con el motivo que verán) a un empleado o a todos los que
        tenían un registro, en una sola sentencia. Cuántos cambiaron."""
        stmt = update(Employee).where(self._scoped())
        if employee_id is not None:
            stmt = stmt.where(Employee.id == employee_id)
        else:
            stmt = stmt.where(Employee.face_status != FaceStatus.NOT_ENROLLED)
        return affected_rows(self.db, stmt.values(face_status=FaceStatus.NOT_ENROLLED, face_rejection_reason=reason))
