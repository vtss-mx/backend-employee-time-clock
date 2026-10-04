"""Dispositivos de los validadores: cada uno se enrola con su propia llave y la empresa lo autoriza.

Inicio de sesión de un validador (si la empresa exige autorizar dispositivos):

1. Sin prueba del dispositivo → 403 `DEVICE_PROOF_REQUIRED` con un reto (`nonce`) firmado por el
   servidor (HMAC, ligado a la cuenta, vence en DEVICE_NONCE_SECONDS).
2. La webapp usa la llave ECDSA P-256 del dispositivo (generada ahí, NO exportable: no se puede
   copiar a otro equipo) para firmar el reto y reintenta con su llave pública y la firma.
3. Firma válida de un dispositivo nuevo → se registra PENDING y responde 403
   `DEVICE_PENDING_APPROVAL`; la empresa lo autoriza (o rechaza) en Validadores › Dispositivos.
4. Solo un dispositivo APPROVED inicia sesión; uno REJECTED o REVOKED responde 403 con su motivo.

Así la contraseña sola no basta: hace falta además un dispositivo que la empresa autorizó. Retirar
la autorización (revocar) cierra las sesiones abiertas del validador.
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.devices import classify_device
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.models import DeviceStatus, SessionRevocationReason, User, UserRole, Validator, ValidatorDevice
from app.repositories.user_repository import UserRepository
from app.repositories.validator_device_repository import ValidatorDeviceRepository
from app.schemas.auth import DeviceProof
from app.schemas.common import PageParams
from app.schemas.validator import ValidatorDeviceList, ValidatorDeviceRead
from app.services.policy_service import PolicyService
from app.services.session_service import SessionService

#: Vigencia del reto que firma el dispositivo (cubre la pausa por el permiso de ubicación).
DEVICE_NONCE_SECONDS = 300

PROOF_REQUIRED = "Este validador solo opera en dispositivos autorizados por la empresa: verificando el dispositivo."
PROOF_INVALID = "No se pudo verificar este dispositivo. Vuelve a iniciar sesión."
PENDING = (
    "Este dispositivo quedó registrado como «{name}» y espera la autorización de tu empresa. "
    "Pide a un administrador que lo autorice en Validadores › Dispositivos."
)
REJECTED = "Tu empresa no autorizó este dispositivo («{name}»). Usa un dispositivo autorizado."
REVOKED = "Tu empresa retiró la autorización de este dispositivo («{name}»). Pide que lo autoricen de nuevo."


def _nonce_key() -> bytes:
    return hashlib.sha256(b"validator-device-nonce:" + settings.DATA_ENCRYPTION_KEY.encode()).digest()


def _nonce_mac(user_id: int, expires: int, salt: str) -> str:
    message = f"{user_id}.{expires}.{salt}".encode()
    return base64.urlsafe_b64encode(hmac.new(_nonce_key(), message, hashlib.sha256).digest()).decode().rstrip("=")


def issue_nonce(user_id: int) -> str:
    """Reto para que el dispositivo lo firme: vence y solo sirve para esta cuenta (sin estado)."""
    expires = int(time.time()) + DEVICE_NONCE_SECONDS
    salt = secrets.token_urlsafe(18)
    return f"{expires}.{salt}.{_nonce_mac(user_id, expires, salt)}"


def nonce_is_valid(user_id: int, nonce: str) -> bool:
    try:
        expires_text, salt, mac = nonce.split(".")
        expires = int(expires_text)
    except ValueError:
        return False
    return expires >= time.time() and hmac.compare_digest(mac, _nonce_mac(user_id, expires, salt))


def _public_key(public_key_b64: str) -> ec.EllipticCurvePublicKey | None:
    try:
        key = serialization.load_der_public_key(base64.b64decode(public_key_b64, validate=True))
    except ValueError, binascii.Error:
        return None
    return key if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1) else None


def signature_is_valid(public_key_b64: str, nonce: str, signature_b64: str) -> bool:
    """Firma ECDSA P-256/SHA-256 del reto (formato de WebCrypto: r||s de 32 bytes cada uno)."""
    key = _public_key(public_key_b64)
    try:
        raw = base64.b64decode(signature_b64, validate=True)
    except ValueError, binascii.Error:
        return False
    if key is None or len(raw) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        key.verify(der, nonce.encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def key_hash(public_key_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()


def _device_name(proof: DeviceProof, user_agent: str | None) -> str:
    if proof.name and proof.name.strip():
        return " ".join(proof.name.split())[:120]
    kind = {"phone": "Teléfono", "tablet": "Tableta", "desktop": "Computadora"}[classify_device(user_agent)]
    return kind


def ensure_device_authorized(
    db: Session, user: User, proof: DeviceProof | None, *, ip: str | None, user_agent: str | None
) -> None:
    """Al iniciar sesión, un validador solo entra desde un dispositivo que su empresa autorizó."""
    validator = user.validator if user.role == UserRole.VALIDATOR else None
    if validator is None or not PolicyService(db, validator.company_id).current().validator_device_approval:
        return
    if proof is None:
        raise PermissionDeniedError(
            PROOF_REQUIRED, code="DEVICE_PROOF_REQUIRED", details={"nonce": issue_nonce(user.id)}
        )
    if not nonce_is_valid(user.id, proof.nonce) or not signature_is_valid(
        proof.public_key, proof.nonce, proof.signature
    ):
        raise PermissionDeniedError(PROOF_INVALID, code="DEVICE_PROOF_INVALID", details={"nonce": issue_nonce(user.id)})

    devices = ValidatorDeviceRepository(db, validator.company_id)
    device = devices.find(validator.id, key_hash(proof.public_key))
    if device is None:
        device = devices.add(
            ValidatorDevice(
                validator_id=validator.id,
                key_hash=key_hash(proof.public_key),
                public_key=proof.public_key,
                name=_device_name(proof, user_agent),
                user_agent=(user_agent or "")[:400] or None,
                last_ip=ip,
                last_seen_at=datetime.now(UTC),
            )
        )
        db.commit()
    details = {"device_id": device.id, "device_name": device.name}
    if device.status == DeviceStatus.PENDING:
        raise PermissionDeniedError(PENDING.format(name=device.name), code="DEVICE_PENDING_APPROVAL", details=details)
    if device.status == DeviceStatus.REJECTED:
        raise PermissionDeniedError(REJECTED.format(name=device.name), code="DEVICE_REJECTED", details=details)
    if device.status == DeviceStatus.REVOKED:
        raise PermissionDeniedError(REVOKED.format(name=device.name), code="DEVICE_REVOKED", details=details)
    device.last_seen_at = datetime.now(UTC)
    device.last_ip = ip
    db.commit()


#: Cambios de estado permitidos: autorizar (desde cualquiera), rechazar uno pendiente, revocar uno autorizado.
TRANSITIONS: dict[DeviceStatus, frozenset[DeviceStatus]] = {
    DeviceStatus.APPROVED: frozenset({DeviceStatus.PENDING, DeviceStatus.REJECTED, DeviceStatus.REVOKED}),
    DeviceStatus.REJECTED: frozenset({DeviceStatus.PENDING}),
    DeviceStatus.REVOKED: frozenset({DeviceStatus.APPROVED}),
}


class DeviceService:
    """La empresa revisa los dispositivos de sus validadores."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.devices = ValidatorDeviceRepository(db, company_id)

    def list(self, validator_id: int, page: PageParams) -> ValidatorDeviceList:
        items, total = self.devices.page(validator_id, offset=page.offset, limit=page.size)
        emails = UserRepository(self.db).emails_by_ids(d.reviewed_by_id for d in items)
        return ValidatorDeviceList.of([self.read(d, emails) for d in items], total, page)

    def read(self, device: ValidatorDevice, emails: dict[int, str] | None = None) -> ValidatorDeviceRead:
        if emails is None:
            emails = UserRepository(self.db).emails_by_ids((device.reviewed_by_id,))
        data = ValidatorDeviceRead.model_validate(device)
        return data.model_copy(
            update={"reviewed_by": emails.get(device.reviewed_by_id) if device.reviewed_by_id else None}
        )

    def set_status(
        self, validator_id: int, device_id: int, status: DeviceStatus, reviewer: User
    ) -> ValidatorDeviceRead:
        device = self.devices.get(validator_id, device_id)
        if device is None:
            raise NotFoundError("Dispositivo no encontrado", code="DEVICE_NOT_FOUND")
        if device.status not in TRANSITIONS[status]:
            raise ConflictError(
                "Ese cambio no aplica al estado actual del dispositivo", code="DEVICE_INVALID_TRANSITION"
            )
        was_approved = device.status == DeviceStatus.APPROVED
        device.status = status
        device.reviewed_at = datetime.now(UTC)
        device.reviewed_by_id = reviewer.id
        validator = self.db.get(Validator, device.validator_id)
        if was_approved and validator is not None:  # sus sesiones ya no tienen un dispositivo autorizado
            SessionService(self.db).revoke_all(validator.user_id, SessionRevocationReason.DEVICE_REVOKED, commit=False)
        self.db.commit()
        return self.read(device)
