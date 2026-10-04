from datetime import datetime
from typing import Literal

from pydantic import BaseModel

#: Estado de un QR dinámico: vigente, ya usado, vencido o reemplazado/invalidado.
QrState = Literal["ACTIVE", "USED", "EXPIRED", "REVOKED"]


class DynamicQrRead(BaseModel):
    """QR recién emitido para que el empleado lo muestre. El teléfono lo dibuja con `content` (el
    servidor no genera imágenes: cada rotación cuesta una consulta, no CPU)."""

    id: int
    employee_number: str
    created_at: datetime
    expires_at: datetime
    #: Vigencia en segundos (política de la empresa): la webapp lo renueva al terminar.
    lifetime_seconds: int
    #: Lo que codifica el QR ("TCQR2:<token>"); sirve una sola vez.
    content: str


class QrStatusRead(BaseModel):
    """Estado de un QR emitido (la webapp lo consulta para renovarlo en cuanto se usa)."""

    id: int
    status: QrState
    expires_at: datetime | None
    used_at: datetime | None


class EmployeeQrSummary(BaseModel):
    """Para la empresa: el empleado muestra su QR desde su teléfono; aquí solo su actividad."""

    #: Hay un QR vigente sin usar en este momento.
    live: bool
    live_until: datetime | None
    last_issued_at: datetime | None
    last_used_at: datetime | None
