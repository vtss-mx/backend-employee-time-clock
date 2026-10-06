from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Row, func, or_, select
from sqlalchemy.orm import Session

from app.models import EmployeeDevice
from app.repositories.aggregates import dialect_insert, paginate

#: Lo que el motor de riesgo necesita de cada fila con la misma llave (sin cargar el modelo completo).
type Sighting = Row[int, str, datetime | None, datetime, int]


class EmployeeDeviceRepository:
    """Dispositivos de los empleados de UNA empresa (solo hashes de llaves)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def sightings(self, key_hash: str, employee_id: int, since: datetime, limit: int) -> Sequence[Sighting]:
        """La fila del empleado con esta llave y las de OTROS empleados que la usaron desde `since` (DEVICE_SHARED),
        en una consulta por el prefijo de `uq_employee_devices_key` (company_id, key_hash). Acotada: un teléfono lo
        comparten pocas personas."""
        stmt = (
            select(
                EmployeeDevice.employee_id,
                EmployeeDevice.status,
                EmployeeDevice.stepped_up_at,
                EmployeeDevice.last_seen_at,
                EmployeeDevice.uses,
            )
            .where(
                EmployeeDevice.company_id == self.company_id,
                EmployeeDevice.key_hash == key_hash,
                or_(EmployeeDevice.employee_id == employee_id, EmployeeDevice.last_seen_at >= since),
            )
            .limit(limit)
        )
        return self.db.execute(stmt).all()

    def record(self, employee_id: int, key_hash: str, name: str, now: datetime, *, stepped_up: bool) -> None:
        """Un uso del dispositivo en UNA sentencia atómica: fila nueva (por decidir) o último uso y un uso más; si el
        intento superó un paso más, desde cuándo es de confianza (lo primero que se anotó se conserva)."""
        stmt = dialect_insert(self.db, EmployeeDevice).values(
            company_id=self.company_id,
            employee_id=employee_id,
            key_hash=key_hash,
            name=name,
            status="PENDING",
            first_seen_at=now,
            last_seen_at=now,
            uses=1,
            stepped_up_at=now if stepped_up else None,
        )
        update: dict[str, Any] = {"last_seen_at": now, "uses": EmployeeDevice.uses + 1}
        if stepped_up:
            update["stepped_up_at"] = func.coalesce(EmployeeDevice.stepped_up_at, now)
        self.db.execute(
            stmt.on_conflict_do_update(index_elements=["company_id", "key_hash", "employee_id"], set_=update)
        )

    def get(self, employee_id: int, device_id: int) -> EmployeeDevice | None:
        device = self.db.get(EmployeeDevice, device_id)
        if device is None or device.company_id != self.company_id or device.employee_id != employee_id:
            return None
        return device

    def page(self, employee_id: int, *, offset: int, limit: int) -> tuple[list[EmployeeDevice], int]:
        """Los del empleado, el más reciente primero (índice company_id + employee_id + first_seen_at + id)."""
        stmt = select(EmployeeDevice).where(
            EmployeeDevice.company_id == self.company_id, EmployeeDevice.employee_id == employee_id
        )
        order = (EmployeeDevice.first_seen_at.desc(), EmployeeDevice.id.desc())
        return paginate(self.db, stmt, order, offset=offset, limit=limit)
