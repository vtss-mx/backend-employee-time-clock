"""Llaves de acceso (WebAuthn / passkeys): inicio de sesión fuerte sin contraseña (antifraude fase 3, I+D §2.6 P6;
decisión del dueño del producto, 2026-10-06).

Cómo funciona (py_webauthn verifica; el servidor solo guarda la llave PÚBLICA):

1. **Registrar** (con sesión): `registration_options` arma el reto y las opciones para el navegador (resident key y
   `user_verification=required`: la llave pide el rostro, la huella o el PIN; sin attestation: las llaves sincronizadas
   de Apple y Google no la dan) y las devuelve con el reto SELLADO (`app/core/sealed.py`, Fernet con una llave derivada
   de `DATA_ENCRYPTION_KEY`; el mismo patrón del reto facial y de las preguntas por voz): el reto no vive en memoria ni
   en la base mientras viaja (N réplicas, PgBouncer). `register` abre el token, exige que sea del mismo usuario y de
   registro, lo marca como USADO con una inserción atómica (`auth.passkey_challenges`: un reto vale una sola vez) y
   verifica la credencial contra el RP ID y los orígenes configurados.
2. **Entrar** (sin sesión): `login_options` (sin lista de llaves: la persona elige en su dispositivo) y
   `authenticate`, que busca la llave por el id de la credencial, detecta copias por el contador de firmas (uno menor
   o igual al guardado cuando alguno es mayor que cero: se REVOCA la llave y se avisa al ADMIN como error del sistema),
   verifica la firma, actualiza contador y último uso, y devuelve la cuenta con sus empleos cargados. La ruta aplica
   después TODO lo del inicio de sesión con contraseña (`ensure_account_usable`, dispositivo, ubicación, límite de
   intentos, la misma sesión).
3. **Listar, renombrar y revocar** las propias (revocar es un borrado real, como una sesión).

Un reto que no es íntegro, de otro uso, de otra persona o vencido responde 422 `PASSKEY_CHALLENGE_INVALID` (al
registrar) o 401 `PASSKEY_LOGIN_FAILED` (al entrar: nunca se dice qué falló); una credencial que no verifica, 422
`PASSKEY_INVALID` o 401 `PASSKEY_LOGIN_FAILED`.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json_dict, parse_authenticator_data
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.core.config import settings
from app.core.exceptions import AuthenticationError, ConflictError, NotFoundError, UnprocessableError
from app.core.sealed import sealer
from app.models import Passkey, User
from app.repositories.passkey_repository import PasskeyRepository
from app.schemas.common import PageParams
from app.schemas.passkey import CREDENTIAL_MAX_BYTES, ChallengeOptions

logger = logging.getLogger(__name__)

#: Cada uso deriva su propia llave: un reto de registro nunca se acepta para entrar, ni al revés.
_REGISTER = sealer(b"passkey-register")
_LOGIN = sealer(b"passkey-login")
#: Lo que la ventana `clientDataJSON` del navegador debe traer firmado (py_webauthn lo compara con el reto).
_CHALLENGE_BYTES = 32


@dataclass(frozen=True)
class Challenge:
    """El reto sellado: sus bytes, quién lo pidió (None al entrar: aún no se sabe) y cuándo vence (época, s)."""

    challenge: bytes
    user_id: int | None
    expires: int


def _seal(sealed: Any, challenge: Challenge) -> str:
    payload = {"c": bytes_to_base64url(challenge.challenge), "u": challenge.user_id, "x": challenge.expires}
    return sealed.encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _open(sealed: Any, token: str, now: int) -> Challenge | None:
    """El reto si es íntegro y no venció; None si no (quien llama decide el código)."""
    try:
        payload = json.loads(sealed.decrypt(token.encode()))
        challenge = Challenge(
            base64url_to_bytes(str(payload["c"])),
            None if payload["u"] is None else int(payload["u"]),
            int(payload["x"]),
        )
    except InvalidToken, ValueError, KeyError, TypeError:
        return None
    return challenge if challenge.expires > now else None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _credential_too_big(credential: dict[str, Any]) -> bool:
    return len(json.dumps(credential, separators=(",", ":"))) > CREDENTIAL_MAX_BYTES


def _sign_count_of(credential: dict[str, Any]) -> int | None:
    """El contador de firmas que trae la respuesta (antes de verificarla: distingue una copia de una firma inválida)."""
    try:
        return int(parse_authenticator_data(base64url_to_bytes(credential["response"]["authenticatorData"])).sign_count)
    except KeyError, TypeError, ValueError, WebAuthnException:
        return None


class PasskeyService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.passkeys = PasskeyRepository(db)

    # ------------------------------------------------------------------ registrar

    def registration_options(self, user: User) -> ChallengeOptions:
        """El reto y las opciones para `navigator.credentials.create`, excluyendo las llaves que ya tiene la cuenta."""
        existing = self.passkeys.for_user(user.id, settings.PASSKEYS_MAX_PER_USER)
        if len(existing) >= settings.PASSKEYS_MAX_PER_USER:
            raise ConflictError(code="PASSKEY_LIMIT_REACHED", params={"count": settings.PASSKEYS_MAX_PER_USER})
        expires = int(time.time()) + settings.PASSKEY_CHALLENGE_TTL_SECONDS
        options = generate_registration_options(
            rp_id=settings.WEBAUTHN_RP_ID,
            rp_name=settings.WEBAUTHN_RP_NAME,
            user_name=user.email,
            user_display_name=user.email,
            # El identificador de la persona para el autenticador: estable y sin datos (su id como texto).
            user_id=str(user.id).encode(),
            timeout=settings.PASSKEY_CHALLENGE_TTL_SECONDS * 1000,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(passkey.credential_id)) for passkey in existing
            ],
        )
        token = _seal(_REGISTER, Challenge(options.challenge, user.id, expires))
        return ChallengeOptions(token=token, options=options_to_json_dict(options))

    def register(self, user: User, token: str, name: str, credential: dict[str, Any]) -> Passkey:
        """Verifica la credencial recién creada y guarda su llave pública. El reto se marca como usado en la misma
        transacción que la llave: repetirlo responde 409."""
        now = datetime.now(UTC)
        challenge = _open(_REGISTER, token, int(now.timestamp()))
        if challenge is None or challenge.user_id != user.id:
            raise UnprocessableError(code="PASSKEY_CHALLENGE_INVALID", field="token")
        if _credential_too_big(credential):
            raise UnprocessableError(code="PASSKEY_INVALID", field="credential")
        if len(self.passkeys.for_user(user.id, settings.PASSKEYS_MAX_PER_USER)) >= settings.PASSKEYS_MAX_PER_USER:
            raise ConflictError(code="PASSKEY_LIMIT_REACHED", params={"count": settings.PASSKEYS_MAX_PER_USER})
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge.challenge,
                expected_rp_id=settings.WEBAUTHN_RP_ID,
                expected_origin=settings.webauthn_origins,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, TypeError, KeyError) as exc:
            raise UnprocessableError(code="PASSKEY_INVALID", field="credential") from exc
        credential_id = bytes_to_base64url(verified.credential_id)
        if self.passkeys.by_credential(credential_id) is not None:
            raise ConflictError(code="PASSKEY_ALREADY_REGISTERED")
        if not self.passkeys.claim_challenge(_digest(token), datetime.fromtimestamp(challenge.expires, UTC)):
            raise ConflictError(code="PASSKEY_CHALLENGE_USED")
        transports = credential.get("response", {}).get("transports") if isinstance(credential, dict) else None
        passkey = Passkey(
            user_id=user.id,
            credential_id=credential_id,
            public_key=bytes_to_base64url(verified.credential_public_key),
            sign_count=verified.sign_count,
            transports=",".join(str(t) for t in transports)[:100] if isinstance(transports, list) else None,
            name=name,
            aaguid=str(verified.aaguid)[:36],
            backup_eligible=verified.credential_device_type == "multi_device",
            backed_up=verified.credential_backed_up,
            created_at=now,
        )
        self.passkeys.add(passkey)
        self.db.commit()
        return passkey

    # ------------------------------------------------------------------ las propias

    def page(self, user: User, page: PageParams) -> tuple[list[Passkey], int]:
        return self.passkeys.page(user.id, offset=page.offset, limit=page.size)

    def rename(self, user: User, passkey_id: int, name: str) -> Passkey:
        passkey = self._own(user, passkey_id)
        passkey.name = name
        self.db.commit()
        return passkey

    def revoke(self, user: User, passkey_id: int) -> None:
        """Revocar es un borrado real: la llave deja de servir para entrar al instante."""
        self.passkeys.delete(self._own(user, passkey_id))
        self.db.commit()

    def _own(self, user: User, passkey_id: int) -> Passkey:
        passkey = self.passkeys.get(passkey_id, user.id)
        if passkey is None:
            raise NotFoundError(code="PASSKEY_NOT_FOUND")
        return passkey

    # ------------------------------------------------------------------ entrar

    def login_options(self) -> ChallengeOptions:
        """El reto para `navigator.credentials.get`, sin lista de llaves (credencial descubrible: la persona elige en su
        dispositivo) y con verificación del usuario obligatoria."""
        expires = int(time.time()) + settings.PASSKEY_CHALLENGE_TTL_SECONDS
        options = generate_authentication_options(
            rp_id=settings.WEBAUTHN_RP_ID,
            timeout=settings.PASSKEY_CHALLENGE_TTL_SECONDS * 1000,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        token = _seal(_LOGIN, Challenge(options.challenge, None, expires))
        return ChallengeOptions(token=token, options=options_to_json_dict(options))

    def authenticate(self, token: str, credential: dict[str, Any]) -> User:
        """La cuenta dueña de la llave que firmó el reto (con sus empleos cargados, como la autenticación de una
        sesión). Toda falla responde 401 `PASSKEY_LOGIN_FAILED` sin decir cuál fue, salvo una copia detectada
        (`PASSKEY_CLONED`: la llave se revoca y el ADMIN se entera). El reto queda usado en la misma transacción."""
        now = datetime.now(UTC)
        challenge = _open(_LOGIN, token, int(now.timestamp()))
        if challenge is None or _credential_too_big(credential):
            raise AuthenticationError(code="PASSKEY_LOGIN_FAILED")
        credential_id = str(credential.get("id") or credential.get("rawId") or "")
        passkey = self.passkeys.by_credential(credential_id) if credential_id else None
        if passkey is None:
            raise AuthenticationError(code="PASSKEY_LOGIN_FAILED")
        self._refuse_clone(passkey, _sign_count_of(credential))
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge.challenge,
                expected_rp_id=settings.WEBAUTHN_RP_ID,
                expected_origin=settings.webauthn_origins,
                credential_public_key=base64url_to_bytes(passkey.public_key),
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, TypeError, KeyError) as exc:
            raise AuthenticationError(code="PASSKEY_LOGIN_FAILED") from exc
        if not self.passkeys.claim_challenge(_digest(token), datetime.fromtimestamp(challenge.expires, UTC)):
            raise AuthenticationError(code="PASSKEY_LOGIN_FAILED", key="PASSKEY_CHALLENGE_USED")
        self.passkeys.touch(passkey.id, verified.new_sign_count, now)
        self.db.commit()
        user = passkey.user
        user.use_company(None)
        return user

    def _refuse_clone(self, passkey: Passkey, sign_count: int | None) -> None:
        """Un contador de firmas que no avanza (cuando alguno es mayor que cero) delata una copia de la llave privada:
        se revoca y el ADMIN se entera (error del sistema); la persona entra con su contraseña y registra otra."""
        if sign_count is None or (sign_count == 0 and passkey.sign_count == 0) or sign_count > passkey.sign_count:
            return
        self.passkeys.delete(passkey)
        self.db.commit()
        logger.error(
            "Llave de acceso #%s de la cuenta #%s revocada: su contador de firmas no avanzó (%s → %s); una copia de la "
            "llave privada pudo haberse usado",
            passkey.id,
            passkey.user_id,
            passkey.sign_count,
            sign_count,
        )
        raise AuthenticationError(code="PASSKEY_CLONED")
