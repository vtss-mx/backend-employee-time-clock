from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import TENANCY, WORKFORCE
from app.models.mixins import SoftDeleteMixin, TimestampMixin, live_unique, trash_index


class Department(SoftDeleteMixin, TimestampMixin, Base):
    """Departamento de una empresa (p. ej. "Producción", "Recursos Humanos").

    Tiene empleados asignados (`employees.department_id`: cada empleado está a lo más en uno) y
    responsables (`department_managers`: empleados de la misma empresa, pueden ser varios).

    Con borrado lógico (migración 0068): solo sin empleados vigentes y su nombre queda libre. Sus responsables se
    conservan ocultos mientras él (o el empleado) está en «Eliminados» y regresan al restaurarlo (2026-10-06).
    """

    __tablename__ = "departments"
    __table_args__ = (
        # Nombre único por empresa entre los VIGENTES, sin distinguir mayúsculas; también da el orden del listado.
        live_unique("uq_departments_company_name", "company_id", text("lower(name)")),
        # Papelera de la empresa (el más reciente primero) y depuración de los eliminados.
        trash_index("departments", "company_id", "deleted_at", "id"),
        # Destino de las FK compuestas: un empleado o un responsable solo puede ser de SU empresa.
        UniqueConstraint("id", "company_id", name="uq_departments_id_company"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))


class DepartmentManager(Base):
    """Responsable de un departamento: un empleado de la misma empresa (la BD lo garantiza).

    Sin borrado lógico propio: eliminar el departamento o el empleado NO borra la relación (las lecturas la unen con
    ese lado, que el borrado lógico oculta) y restaurarlos la regresa tal cual; la depuración del año la borra en
    cascada con cualquiera de los dos. Quitar a un responsable sí la borra (no es eliminar un registro)."""

    __tablename__ = "department_managers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["department_id", "company_id"],
            [f"{WORKFORCE}.departments.id", f"{WORKFORCE}.departments.company_id"],
            name="fk_department_managers_department_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["employee_id", "company_id"],
            [f"{WORKFORCE}.employees.id", f"{WORKFORCE}.employees.company_id"],
            name="fk_department_managers_employee_company",
            ondelete="CASCADE",
        ),
        # Departamentos que dirige un empleado (su expediente) y borrado en cascada del empleado.
        Index("ix_department_managers_employee", "employee_id"),
        {"schema": WORKFORCE},
    )

    department_id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
