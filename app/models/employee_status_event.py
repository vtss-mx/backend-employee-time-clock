from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import TENANCY, WORKFORCE


class EmployeeStatusEvent(Base):
    """Historial de altas, bajas y reactivaciones de cada empleo: cuándo quedó activo o inactivo.

    Es la fuente de cuántos empleados activos tuvo cada empresa cada día (`billing.headcount_days`,
    la base del cobro prorrateado): con ella el mantenimiento recalcula, igual y cuantas veces haga
    falta, un día que no pudo cerrar (instancia caída). Lo escribe `EmployeeService` en la misma
    transacción del alta, la activación, la desactivación o la eliminación.

    `employee_id` va SIN llave foránea a propósito: eliminar a un empleado no borra los días que estuvo
    activo (ya se cobraron o se cobrarán en el periodo). La empresa sí (CASCADE): sin empresa no hay
    cobro. Solo inserciones: nunca se actualiza una fila.

    Índice `(company_id, employee_id, occurred_at, id)` INCLUDE `active`: el estado de cada empleado al
    empezar un día es su último evento anterior (ventana por empleado en el orden del índice, sin
    ordenar en memoria); su prefijo `company_id` sirve también a la FK.
    """

    __tablename__ = "employee_status_events"
    __table_args__ = (
        Index(
            "ix_employee_status_events_company_employee",
            "company_id",
            "employee_id",
            "occurred_at",
            "id",
            postgresql_include=["active"],
        ),
        # Altas y reactivaciones de un día (quien entró a mitad del día cuenta ese día).
        Index("ix_employee_status_events_occurred_at", "occurred_at"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    #: True: quedó activo (alta o reactivación). False: quedó inactivo (baja o eliminación).
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
