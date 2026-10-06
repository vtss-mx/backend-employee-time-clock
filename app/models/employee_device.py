from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY, WORKFORCE
from app.models.mixins import company_fk


class EmployeeDevice(Base):
    """Dispositivo (navegador de un teléfono o computadora) desde el que un empleado checa o verifica su identidad.

    Antifraude 1b (decisión D2 del dueño del producto; `app/services/employee_devices.py`). Igual que los validadores,
    la app genera en el navegador una llave ECDSA P-256 NO exportable (no se puede copiar a otro equipo) y firma un reto
    del servidor con cada registro. Aquí se guarda SOLO el hash de su llave pública (la llave viaja con cada firma: la
    posesión se prueba en cada intento), un nombre derivado del navegador y cuándo se vio por primera y última vez.

    `status` es la decisión de la empresa (catálogo `device_statuses`): `PENDING` (sin decidir), `APPROVED` o
    `REVOKED`; `stepped_up_at`, cuándo superó en él un paso más (modo STEP_UP). Una fila por empleado y dispositivo:
    la misma llave en dos empleados es el mismo teléfono (señal DEVICE_SHARED).
    """

    __tablename__ = "employee_devices"
    __table_args__ = (
        # Un dispositivo por empleado (inserción atómica de cada uso: ON CONFLICT) y, por su prefijo, "¿quién más de
        # la empresa usó esta llave?" (DEVICE_SHARED, una consulta por intento).
        UniqueConstraint("company_id", "key_hash", "employee_id", name="uq_employee_devices_key"),
        # Los dispositivos de un empleado (su ficha y Mi perfil), del más reciente al más antiguo; también sirve a la
        # FK compuesta hacia el empleado (borrarlo recorre solo sus filas).
        Index("ix_employee_devices_employee", "company_id", "employee_id", "first_seen_at", "id"),
        Index(
            "ix_employee_devices_reviewed_by_id",
            "reviewed_by_id",
            postgresql_where=text("reviewed_by_id IS NOT NULL"),
            sqlite_where=text("reviewed_by_id IS NOT NULL"),
        ),
        # El empleado es de la MISMA empresa del dispositivo (FK compuesta).
        company_fk("employee_devices", "employee_id", f"{WORKFORCE}.employees"),
        # Cada uso anota la última vez y suma a `uses`: espacio libre para hacerlo en su página (HOT).
        {"schema": WORKFORCE, "postgresql_with": {"fillfactor": 90}},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[int] = mapped_column(nullable=False)
    #: SHA-256 de la llave pública (SPKI): identifica al dispositivo sin guardar la llave.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Nombre derivado del navegador ("iPhone · Safari") o la llave de su tipo (se traduce al leerse).
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(f"{CATALOG}.device_statuses.code"),
        default="PENDING",
        server_default="PENDING",
        nullable=False,
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    #: Intentos registrados desde él (registros y verificaciones).
    uses: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    #: Superó un paso más en este dispositivo (modo STEP_UP: desde entonces es de confianza).
    stepped_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
