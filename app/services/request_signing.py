"""Firma por petición de los validadores (antifraude fase 2b; docs/rd/antifraude-identidad.md §2.6, §5.2 y §7.1).

Hasta la fase 2a, el dispositivo de un validador probaba su llave ECDSA P-256 NO exportable (`utils/deviceKey.ts`)
solo al iniciar sesión: después, el token de 12 h servía desde cualquier equipo (riesgo R7). Ahora cada identificación
(rostro 1:N, QR y QR + rostro) lleva una firma FRESCA de esa misma llave sobre `"{reto}.{acción}.{huella}"`:

- el **reto** es el de siempre (`device_service.issue_nonce`: HMAC con la cuenta y su vencimiento; sin estado, sirve
  con N réplicas y PgBouncer). El servidor manda el siguiente con cada respuesta (`device_nonce` del perfil, del
  resultado de cada identificación y de los errores de la firma), así que firmar no cuesta otra ida y vuelta: la app
  solo pide uno nuevo si el que tiene está por vencer (un kiosco ocioso más de `DEVICE_NONCE_SECONDS`);
- la **acción** (`face`, `qr`, `inspect`) y la **huella** (SHA-256 de la primera captura frontal o del texto del QR)
  atan la firma a ESA petición: no sirve para otra identificación, y lo que hace única a cada una es su contenido, que
  ya es de un solo uso (el reto de la prueba de vida, el QR, la huella anti-reenvío de la captura);
- la **llave** debe ser la de la sesión (`auth_sessions.device_key_hash`, ligada al iniciar sesión con la prueba del
  dispositivo). Una sesión sin llave (empresa sin aprobación de dispositivos, o anterior a la migración 0070) queda
  ligada a la primera llave que firma (una sentencia atómica, una vez por sesión); con la aprobación de dispositivos,
  solo a uno APROBADO de este validador.

Veredictos: firmada, sin firma (`MISSING`), con el reto vencido pero auténtico (`STALE`), alterada (`INVALID`: la firma
no corresponde o el reto no lo emitió el servidor para esta cuenta) o de otro dispositivo (`MISMATCH`). Según la
política (`verification_policy.validator_signing`, modos de `signal_modes`):

- `OFF`: no se revisa;
- `OBSERVE` (por omisión): cada veredicto que no es "firmada" es una señal del motor de riesgo (`VALIDATOR_UNSIGNED`,
  `VALIDATOR_SIGNATURE_INVALID` —regla dura, también nace en «Solo medir»— y `VALIDATOR_KEY_MISMATCH`); nunca rechaza;
- `ENFORCE`: 403 con su código (`SIGNATURE_REQUIRED`, `SIGNATURE_STALE`, `SIGNATURE_INVALID`,
  `SIGNATURE_KEY_MISMATCH`) ANTES de consumir el reto o el QR (la app puede reintentar con lo mismo) y con un reto nuevo
  en `details.device_nonce`.

Costo por identificación: una verificación ECDSA (≈0.1 ms) y un HMAC; cero consultas (la llave de la sesión la carga la
autenticación con la sesión misma: `User.session_device_key`), salvo la primera firma de una sesión sin llave (una
lectura del dispositivo aprobado y una actualización, una sola vez).
"""

import hashlib
import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from app.core.exceptions import PermissionDeniedError
from app.models import DeviceStatus, RiskSignal, SignalMode, Validator
from app.repositories.session_repository import SessionRepository
from app.repositories.validator_device_repository import ValidatorDeviceRepository
from app.services.device_service import NonceState, issue_nonce, key_hash, nonce_state, signature_is_valid
from app.services.policy_service import PolicySnapshot
from app.services.risk_rules import Hit

logger = logging.getLogger(__name__)

#: Lo que firma cada identificación (la acción va dentro del mensaje: una firma de un QR no sirve para un rostro).
ACTION_FACE = "face"
ACTION_QR = "qr"
ACTION_INSPECT = "inspect"
#: Largos máximos de lo que llega (lo demás no es de la app: alterado).
MAX_KEY_CHARS = 300
MAX_NONCE_CHARS = 200
MAX_SIGNATURE_CHARS = 200


class SignatureVerdict(StrEnum):
    SIGNED = "SIGNED"
    MISSING = "MISSING"
    STALE = "STALE"
    INVALID = "INVALID"
    MISMATCH = "MISMATCH"


#: La señal de cada veredicto (`value` distingue sin firma = 0 de reto vencido = 1).
VERDICT_HITS = {
    SignatureVerdict.MISSING: Hit(RiskSignal.VALIDATOR_UNSIGNED, 0.0),
    SignatureVerdict.STALE: Hit(RiskSignal.VALIDATOR_UNSIGNED, 1.0),
    SignatureVerdict.INVALID: Hit(RiskSignal.VALIDATOR_SIGNATURE_INVALID),
    SignatureVerdict.MISMATCH: Hit(RiskSignal.VALIDATOR_KEY_MISMATCH),
}
#: El código del rechazo de cada veredicto con la firma obligatoria.
VERDICT_ERRORS = {
    SignatureVerdict.MISSING: "SIGNATURE_REQUIRED",
    SignatureVerdict.STALE: "SIGNATURE_STALE",
    SignatureVerdict.INVALID: "SIGNATURE_INVALID",
    SignatureVerdict.MISMATCH: "SIGNATURE_KEY_MISMATCH",
}


@dataclass(frozen=True)
class RequestProof:
    """La firma que trae una identificación: la llave pública (SPKI DER, base64), el reto que firmó y la firma (r||s,
    base64). Vacía si la app no la mandó (sin llave en el navegador o la empresa no la pide)."""

    public_key: str | None = None
    nonce: str | None = None
    signature: str | None = None

    @property
    def sent(self) -> bool:
        return bool(self.public_key or self.nonce or self.signature)

    @property
    def complete(self) -> bool:
        return bool(self.public_key and self.nonce and self.signature)

    @property
    def bounded(self) -> bool:
        return (
            len(self.public_key or "") <= MAX_KEY_CHARS
            and len(self.nonce or "") <= MAX_NONCE_CHARS
            and len(self.signature or "") <= MAX_SIGNATURE_CHARS
        )


def digest_of(data: bytes) -> str:
    """La huella de lo que se identifica (SHA-256 en hexadecimal): la primera captura frontal o el texto del QR."""
    return hashlib.sha256(data).hexdigest()


def signed_message(nonce: str, action: str, digest: str) -> str:
    """Lo que firma la llave del dispositivo: el reto, la acción y la huella de la petición."""
    return f"{nonce}.{action}.{digest}"


class RequestSigning:
    """La firma por petición de las identificaciones de UN validador en su sesión."""

    def __init__(
        self,
        db: Session,
        validator: Validator,
        session: tuple[str | None, str | None],
        policy: PolicySnapshot,
    ) -> None:
        """`session`: el id de la sesión de la petición y la llave a la que ya estaba ligada (la carga la autenticación:
        `User.session_device_key`; None si aún no tiene)."""
        self.db = db
        self.validator = validator
        self.user_id = validator.user_id
        self.session_id, self.bound_key = session
        self.policy = policy

    @property
    def mode(self) -> str:
        return self.policy.validator_signing

    def next_nonce(self) -> str | None:
        """El reto para la siguiente identificación (viaja con cada respuesta); None si la empresa no pide firma."""
        return None if self.mode == SignalMode.OFF else issue_nonce(self.user_id)

    def check(self, proof: RequestProof, action: str, digest: str) -> list[Hit]:
        """Las señales de la firma de esta petición (vacío si va firmada o la empresa no la revisa). Con la firma
        obligatoria, lo que no va firmado se rechaza aquí (403), antes de consumir nada."""
        if self.mode == SignalMode.OFF:
            return []
        verdict = self.verdict(proof, action, digest)
        if verdict == SignatureVerdict.SIGNED:
            return []
        if self.mode == SignalMode.ENFORCE:
            raise PermissionDeniedError(
                code=VERDICT_ERRORS[verdict], details={"device_nonce": issue_nonce(self.user_id)}
            )
        # Solo medir: queda como señal (y en el log del proceso, sin datos de la firma).
        logger.info("Identificación del validador %s con la firma %s (solo medir)", self.validator.id, verdict.value)
        return [VERDICT_HITS[verdict]]

    def verdict(self, proof: RequestProof, action: str, digest: str) -> SignatureVerdict:
        if not proof.complete:
            return SignatureVerdict.INVALID if proof.sent else SignatureVerdict.MISSING
        # `complete` garantiza los tres; mypy no lo deduce de la propiedad.
        public_key, nonce, signature = str(proof.public_key), str(proof.nonce), str(proof.signature)
        if not proof.bounded:
            return SignatureVerdict.INVALID
        state = nonce_state(self.user_id, nonce)
        if state == NonceState.INVALID or not signature_is_valid(
            public_key, signed_message(nonce, action, digest), signature
        ):
            return SignatureVerdict.INVALID
        if state == NonceState.EXPIRED:
            return SignatureVerdict.STALE
        return SignatureVerdict.SIGNED if self._bound(key_hash(public_key)) else SignatureVerdict.MISMATCH

    def _bound(self, key: str) -> bool:
        """¿La llave es la de la sesión? Una sesión sin llave queda ligada a esta (una vez; con la aprobación de
        dispositivos, solo si es un dispositivo aprobado de este validador)."""
        # La llave de la sesión la cargó la autenticación con la sesión misma: no hay consulta.
        if self.bound_key is not None:
            return self.bound_key == key
        if self.session_id is None:
            return False
        if self.policy.validator_device_approval:
            device = ValidatorDeviceRepository(self.db, self.validator.company_id).find(self.validator.id, key)
            if device is None or device.status != DeviceStatus.APPROVED:
                return False
        return SessionRepository(self.db).bind_device(self.session_id, key)
