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

Antifraude 2b: la sesión queda LIGADA a la llave que probó el inicio de sesión (`auth_sessions.device_key_hash`) y
cada identificación vuelve a firmar con ella un reto del servidor junto con su contenido (`request_signing`): el token
solo, copiado a otro equipo, ya no identifica a nadie.
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime
from enum import StrEnum

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.devices import classify_device
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.i18n import read_stored, stored
from app.models import DeviceStatus, SessionRevocationReason, SignalMode, User, UserRole, Validator, ValidatorDevice
from app.repositories.user_repository import UserRepository
from app.repositories.validator_device_repository import ValidatorDeviceRepository
from app.schemas.auth import DeviceProof
from app.schemas.common import PageParams
from app.schemas.validator import ValidatorDeviceList, ValidatorDeviceRead
from app.services.policy_service import PolicyService
from app.services.session_service import SessionService

#: Vigencia del reto que firma el dispositivo (cubre la pausa por el permiso de ubicación).
DEVICE_NONCE_SECONDS = 300

#: Nombre que recibe un dispositivo que no dio el suyo: la llave de su tipo, guardada para traducirse al leerse
#: (`app/i18n/stored.py`; la lista de la empresa y los mensajes lo dicen en el idioma de quien lee).
_DEVICE_KINDS = {"phone": "DEVICE_PHONE", "tablet": "DEVICE_TABLET", "desktop": "DEVICE_DESKTOP"}


def _nonce_key() -> bytes:
    return hashlib.sha256(b"validator-device-nonce:" + settings.DATA_ENCRYPTION_KEY.encode()).digest()


def _nonce_mac(subject: int, expires: int, salt: str, purpose: str) -> str:
    # Sin propósito, el mensaje de siempre (cuentas: validadores y empleados); con él, otro espacio de nombres (p. ej.
    # el de los kioscos: el reto del kiosco 5 nunca sirve como el de la cuenta 5).
    prefix = f"{purpose}:" if purpose else ""
    message = f"{prefix}{subject}.{expires}.{salt}".encode()
    return base64.urlsafe_b64encode(hmac.new(_nonce_key(), message, hashlib.sha256).digest()).decode().rstrip("=")


def issue_nonce(subject: int, *, purpose: str = "") -> str:
    """Reto para que el dispositivo lo firme: vence y solo sirve para esta cuenta (o este kiosco, con su `purpose`),
    sin estado. Su forma es `{vence}.{sal}.{mac}`: el cliente sabe cuándo vence sin preguntar."""
    expires = int(time.time()) + DEVICE_NONCE_SECONDS
    salt = secrets.token_urlsafe(18)
    return f"{expires}.{salt}.{_nonce_mac(subject, expires, salt, purpose)}"


class NonceState(StrEnum):
    """Un reto recibido: lo emitió el servidor para este sujeto y sigue vigente, ya venció (auténtico pero viejo) o no
    lo emitió el servidor para él (alterado, de otra cuenta o mal formado)."""

    VALID = "VALID"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"


def nonce_state(subject: int, nonce: str, *, purpose: str = "") -> NonceState:
    try:
        expires_text, salt, mac = nonce.split(".")
        expires = int(expires_text)
    except ValueError:
        return NonceState.INVALID
    if not hmac.compare_digest(mac, _nonce_mac(subject, expires, salt, purpose)):
        return NonceState.INVALID
    return NonceState.VALID if expires >= time.time() else NonceState.EXPIRED


def nonce_is_valid(user_id: int, nonce: str) -> bool:
    return nonce_state(user_id, nonce) == NonceState.VALID


def _public_key(public_key_b64: str) -> ec.EllipticCurvePublicKey | None:
    try:
        key = serialization.load_der_public_key(base64.b64decode(public_key_b64, validate=True))
    except ValueError, binascii.Error:
        return None
    return key if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1) else None


def public_key_is_valid(public_key_b64: str) -> bool:
    """Una llave pública ECDSA P-256 (SPKI DER, base64) que sirve para verificar firmas."""
    return _public_key(public_key_b64) is not None


def signature_is_valid(public_key_b64: str, message: str, signature_b64: str) -> bool:
    """Firma ECDSA P-256/SHA-256 de `message` (el reto al iniciar sesión; el reto con la petición en la firma por
    petición) en el formato de WebCrypto: r||s de 32 bytes cada uno."""
    key = _public_key(public_key_b64)
    try:
        raw = base64.b64decode(signature_b64, validate=True)
    except ValueError, binascii.Error:
        return False
    if key is None or len(raw) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        key.verify(der, message.encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def key_hash(public_key_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()


def _device_name(proof: DeviceProof, user_agent: str | None) -> str:
    if proof.name and proof.name.strip():
        return " ".join(proof.name.split())[:120]
    return stored(_DEVICE_KINDS[classify_device(user_agent)])


def ensure_device_authorized(
    db: Session, user: User, proof: DeviceProof | None, *, ip: str | None, user_agent: str | None
) -> str | None:
    """Al iniciar sesión, un validador solo entra desde un dispositivo que su empresa autorizó. Devuelve el hash de la
    llave probada (la sesión queda ligada a ella: cada identificación debe firmarla, `request_signing`), o None si no
    se pidió prueba.

    Con la firma por petición OBLIGATORIA y sin aprobación de dispositivos, también se pide la prueba (solo la posesión
    de la llave, sin registrar el dispositivo): así la sesión nace ligada y un token copiado no sirve en otro equipo."""
    validator = user.validator if user.role == UserRole.VALIDATOR else None
    if validator is None:
        return None
    policy = PolicyService(db, validator.company_id).current()
    approval = policy.validator_device_approval
    if not approval and policy.validator_signing != SignalMode.ENFORCE:
        return None
    if proof is None:
        raise PermissionDeniedError(code="DEVICE_PROOF_REQUIRED", details={"nonce": issue_nonce(user.id)})
    if not nonce_is_valid(user.id, proof.nonce) or not signature_is_valid(
        proof.public_key, proof.nonce, proof.signature
    ):
        raise PermissionDeniedError(code="DEVICE_PROOF_INVALID", details={"nonce": issue_nonce(user.id)})
    if not approval:
        return key_hash(proof.public_key)

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
    name = read_stored(device.name)
    details = {"device_id": device.id, "device_name": name}
    if device.status == DeviceStatus.PENDING:
        raise PermissionDeniedError(code="DEVICE_PENDING_APPROVAL", params={"name": name}, details=details)
    if device.status == DeviceStatus.REJECTED:
        raise PermissionDeniedError(code="DEVICE_REJECTED", params={"name": name}, details=details)
    if device.status == DeviceStatus.REVOKED:
        raise PermissionDeniedError(code="DEVICE_REVOKED", params={"name": name}, details=details)
    device.last_seen_at = datetime.now(UTC)
    device.last_ip = ip
    db.commit()
    return device.key_hash


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
            raise NotFoundError(code="DEVICE_NOT_FOUND")
        if device.status not in TRANSITIONS[status]:
            raise ConflictError(code="DEVICE_INVALID_TRANSITION")
        was_approved = device.status == DeviceStatus.APPROVED
        device.status = status
        device.reviewed_at = datetime.now(UTC)
        device.reviewed_by_id = reviewer.id
        validator = self.db.get(Validator, device.validator_id)
        if was_approved and validator is not None:  # sus sesiones ya no tienen un dispositivo autorizado
            SessionService(self.db).revoke_all(validator.user_id, SessionRevocationReason.DEVICE_REVOKED, commit=False)
        self.db.commit()
        return self.read(device)
