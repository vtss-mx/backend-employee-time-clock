"""Dispositivos de un empleado (antifraude 1b, decisión D2): lo que ven la empresa (ficha del empleado) y el empleado
(Mi perfil). Nunca la llave: solo su nombre amigable, su estado y cuándo se usó."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.i18n import StoredText
from app.models import DeviceStatus
from app.schemas.common import Page


class EmployeeDeviceRead(BaseModel):
    """Un dispositivo desde el que el empleado checó o verificó su identidad."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    #: "iPhone · Safari" (del navegador) o su tipo en el idioma de quien lee ("Teléfono", "Phone").
    name: StoredText
    #: Decisión de la empresa (catálogo `device_statuses`): por decidir, aprobado o revocado.
    status: str
    first_seen_at: datetime
    last_seen_at: datetime
    uses: int
    #: Cuándo superó en él un paso más (modo «Un paso más en un dispositivo nuevo»: desde entonces es de confianza).
    stepped_up_at: datetime | None = None
    reviewed_at: datetime | None = None
    #: Quién decidió (solo en la ficha que ve la empresa).
    reviewed_by: str | None = None


class EmployeeDeviceList(Page[EmployeeDeviceRead]):
    """Página de dispositivos de un empleado (el más reciente primero)."""


class EmployeeDeviceStatusUpdate(BaseModel):
    """Aprobar (los registros desde él dejan de quedar en revisión) o revocar (vuelve a ser desconocido)."""

    status: Literal[DeviceStatus.APPROVED, DeviceStatus.REVOKED]
