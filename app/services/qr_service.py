"""Códigos QR DINÁMICOS de los empleados: duran unos segundos y sirven una sola vez.

Contenido del QR:  "TCQR2:<token>"
- <token> = 24 bytes aleatorios (192 bits). No contiene ID, contraseña ni datos biométricos.
- En la BD solo se guarda SHA-256(token): ni la empresa puede volver a dibujar un QR emitido.

Ciclo de vida:
1. El empleado abre "Mi código QR": se emite uno nuevo (el anterior queda reemplazado) que vence a
   los `qr_lifetime_seconds` de la política de su empresa. Al vencer o al usarse, la webapp pide
   otro; también "bajo demanda" (botón) y la empresa puede invalidar el vigente.
2. Un validador lo escanea: se marca usado en una sola sentencia atómica (dos lecturas simultáneas
   no pueden ganar ambas). Un QR usado, vencido o reemplazado no vuelve a servir NUNCA.
3. QR + rostro: el escaneo aparta el QR para ese validador y el rostro lo completa (una vez,
   dentro de QR_FACE_WINDOW_SECONDS).

Los QR fijos anteriores ("TCQR1:", impresos o descargados) ya no se aceptan (motivo STATIC_QR).
"""

import base64
import io
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.crypto import hash_token
from app.core.exceptions import NotFoundError
from app.models import Employee, EmployeeQr
from app.repositories.qr_repository import EmployeeQrRepository
from app.schemas.qr import DynamicQrRead, EmployeeQrSummary, QrState, QrStatusRead

QR_PREFIX = "TCQR2:"
#: QR fijo de la versión anterior (credencial impresa): se rechaza con un motivo propio.
LEGACY_PREFIX = "TCQR1:"
TOKEN_BYTES = 24
_MAX_TOKEN_LENGTH = 128


@dataclass(frozen=True)
class QrUse:
    """Resultado de usar un QR: el empleado dueño, o el motivo del rechazo (verification_reasons)."""

    employee: Employee | None
    reason: str | None = None


def qr_state(qr: EmployeeQr, now: datetime | None = None) -> QrState:
    if qr.used_at is not None:
        return "USED"
    if not qr.active:
        return "REVOKED"
    if qr.expires_at is None or as_utc(qr.expires_at) <= (now or datetime.now(UTC)):
        return "EXPIRED"
    return "ACTIVE"


#: Motivo de la bitácora para cada estado que impide usar el QR.
_REJECTION: dict[QrState, str] = {"USED": "ALREADY_USED", "REVOKED": "REVOKED", "EXPIRED": "EXPIRED"}


class QrService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = EmployeeQrRepository(db)

    # ---------- Empleado: emitir y seguir su QR ----------

    def issue(self, employee: Employee, lifetime_seconds: int) -> tuple[EmployeeQr, str]:
        """Reemplaza el QR vigente del empleado por uno nuevo; devuelve el registro y su contenido."""
        now = datetime.now(UTC)
        self.repo.purge_expired(now - timedelta(days=settings.QR_TOKEN_RETENTION_DAYS))
        self.repo.revoke_all_for_employee(employee.id)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        qr = self.repo.add(
            EmployeeQr(
                employee_id=employee.id,
                token_hash=hash_token(token),
                active=True,
                expires_at=now + timedelta(seconds=lifetime_seconds),
            )
        )
        return qr, QR_PREFIX + token

    def to_read(self, employee: Employee, qr: EmployeeQr, content: str, lifetime_seconds: int) -> DynamicQrRead:
        return DynamicQrRead(
            id=qr.id,
            employee_number=employee.employee_number,
            created_at=qr.created_at,
            expires_at=as_utc(qr.expires_at) or datetime.now(UTC),
            lifetime_seconds=lifetime_seconds,
            image_base64="data:image/png;base64," + self._render_png(content),
        )

    def status(self, employee: Employee, qr_id: int) -> QrStatusRead:
        qr = self.repo.get_for_employee(employee.id, qr_id)
        if qr is None:
            raise NotFoundError("Código QR no encontrado", code="QR_NOT_FOUND")
        return QrStatusRead(id=qr.id, status=qr_state(qr), expires_at=qr.expires_at, used_at=qr.used_at)

    # ---------- Empresa ----------

    def summary(self, employee: Employee) -> EmployeeQrSummary:
        issued = self.repo.latest_issued(employee.id)
        used = self.repo.latest_used(employee.id)
        live = issued is not None and qr_state(issued) == "ACTIVE"
        return EmployeeQrSummary(
            live=live,
            live_until=issued.expires_at if live and issued else None,
            last_issued_at=issued.created_at if issued else None,
            last_used_at=used.used_at if used else None,
        )

    def revoke(self, employee: Employee) -> None:
        """Invalida el QR vigente (la webapp del empleado muestra otro de inmediato)."""
        self.repo.revoke_all_for_employee(employee.id)

    # ---------- Punto de control: usar un QR ----------

    def use(self, qr_content: str, *, company_id: int, actor_id: int, hold: bool = False) -> QrUse:
        """Consume el QR (una sola vez). Con `hold`, queda apartado para confirmar el rostro."""
        qr, reason = self._find(qr_content, company_id)
        if qr is None:
            return QrUse(None, reason)
        state = qr_state(qr)
        if state != "ACTIVE":
            return QrUse(None, _REJECTION[state])
        if not qr.employee.active:
            return QrUse(None, "EMPLOYEE_INACTIVE")
        if not self.repo.mark_used(qr.id, actor_id, complete=not hold):
            return QrUse(None, "ALREADY_USED")  # otra lectura simultánea ganó
        return QrUse(qr.employee)

    def complete_hold(self, qr_content: str, *, company_id: int, actor_id: int) -> QrUse:
        """QR + rostro: cierra el QR que ESTE validador apartó al escanearlo (una vez, en la ventana).
        Si aún no se había escaneado, se usa y cierra aquí mismo."""
        qr, reason = self._find(qr_content, company_id)
        if qr is None:
            return QrUse(None, reason)
        if qr.used_at is None:
            return self.use(qr_content, company_id=company_id, actor_id=actor_id)
        window = timedelta(seconds=settings.QR_FACE_WINDOW_SECONDS)
        if qr.used_by_id != actor_id or qr.completed_at is not None:
            return QrUse(None, "ALREADY_USED")
        if as_utc(qr.used_at) + window <= datetime.now(UTC):
            return QrUse(None, "EXPIRED")
        if not self.repo.mark_completed(qr.id, actor_id):
            return QrUse(None, "ALREADY_USED")
        return QrUse(qr.employee)

    # ---------- Internos ----------

    def _find(self, qr_content: str, company_id: int) -> tuple[EmployeeQr | None, str | None]:
        content = qr_content.strip()
        if content.startswith(LEGACY_PREFIX):
            return None, "STATIC_QR"
        if not content.startswith(QR_PREFIX):
            return None, "INVALID_FORMAT"
        token = content[len(QR_PREFIX) :]
        if not token or len(token) > _MAX_TOKEN_LENGTH:
            return None, "INVALID_FORMAT"
        qr = self.repo.get_by_token_hash(hash_token(token))
        if qr is None:
            return None, "NOT_FOUND"
        if qr.employee.company_id != company_id:
            return None, "OTHER_COMPANY"  # se informa como "QR no reconocido" y no se consume
        return qr, None

    @staticmethod
    def _render_png(data: str) -> str:
        qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode()
