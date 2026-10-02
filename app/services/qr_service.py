"""Generación, consulta, regeneración y validación de códigos QR de empleados.

Contenido del QR:  "TCQR1:<token>"
- <token> = 32 bytes aleatorios (secrets.token_urlsafe), 256 bits de entropía.
- No contiene ID de empleado, contraseña ni datos biométricos.
- En BD se guarda SHA-256(token) para la búsqueda y el token cifrado (Fernet)
  únicamente para poder re-dibujar la imagen al administrador.
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
from app.core.crypto import decrypt_bytes, encrypt_bytes, hash_token
from app.core.exceptions import NotFoundError
from app.models import Employee, EmployeeQr
from app.repositories.qr_repository import EmployeeQrRepository
from app.schemas.qr import EmployeeQrRead

QR_PREFIX = "TCQR1:"
TOKEN_BYTES = 32
_MAX_TOKEN_LENGTH = 128


@dataclass(frozen=True)
class QrLookup:
    qr: EmployeeQr | None
    reason: str | None  # None = válido


class QrService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = EmployeeQrRepository(db)

    # ---------- Administración ----------

    def issue(self, employee: Employee) -> EmployeeQr:
        """Revoca los QR activos del empleado y emite uno nuevo."""
        self.repo.revoke_all_for_employee(employee.id)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        expires_at = (
            datetime.now(UTC) + timedelta(days=settings.QR_TOKEN_EXPIRE_DAYS)
            if settings.QR_TOKEN_EXPIRE_DAYS > 0
            else None
        )
        return self.repo.add(
            EmployeeQr(
                employee_id=employee.id,
                token_hash=hash_token(token),
                token_encrypted=encrypt_bytes(token.encode()),
                active=True,
                expires_at=expires_at,
            )
        )

    def revoke(self, employee: Employee) -> None:
        self.repo.revoke_all_for_employee(employee.id)

    def get_active(self, employee: Employee) -> EmployeeQr:
        qr = self.repo.get_active_for_employee(employee.id)
        if qr is None:
            raise NotFoundError("El empleado no tiene un QR activo. Regenéralo.", code="QR_NOT_FOUND")
        return qr

    def to_read(self, employee: Employee, qr: EmployeeQr) -> EmployeeQrRead:
        token = decrypt_bytes(qr.token_encrypted).decode()
        return EmployeeQrRead(
            id=qr.id,
            employee_id=employee.id,
            employee_number=employee.employee_number,
            active=qr.active,
            created_at=qr.created_at,
            expires_at=qr.expires_at,
            image_base64="data:image/png;base64," + self._render_png(QR_PREFIX + token),
            file_name=f"qr-{employee.employee_number}.png",
        )

    # ---------- Verificación ----------

    def lookup(self, qr_content: str) -> QrLookup:
        content = qr_content.strip()
        if not content.startswith(QR_PREFIX):
            return QrLookup(None, "INVALID_FORMAT")
        token = content[len(QR_PREFIX) :]
        if not token or len(token) > _MAX_TOKEN_LENGTH:
            return QrLookup(None, "INVALID_FORMAT")

        qr = self.repo.get_by_token_hash(hash_token(token))
        if qr is None:
            return QrLookup(None, "NOT_FOUND")
        if not qr.active:
            return QrLookup(qr, "REVOKED")
        if qr.expires_at is not None and as_utc(qr.expires_at) <= datetime.now(UTC):
            return QrLookup(qr, "EXPIRED")
        return QrLookup(qr, None)

    # ---------- Utilidades ----------

    @staticmethod
    def _render_png(data: str) -> str:
        qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode()
