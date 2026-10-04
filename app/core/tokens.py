"""Tokens de acceso JWT (RFC 7519 / perfil RFC 9068) y llaves de firma.

- ES256 (ECDSA P-256) por defecto: firma asimétrica. La llave pública se publica como JWKS
  (`GET /api/auth/jwks`), así otros servicios pueden validar tokens sin conocer secretos.
- Cada token lleva `kid` (huella RFC 7638 de la llave): permite rotar llaves sin invalidar
  sesiones; las llaves anteriores se configuran en JWT_PREVIOUS_KEYS solo para validar.
- Claims: iss, aud, sub, sid (sesión revocable), jti, role, iat, nbf, exp; cabecera typ=at+jwt.
- Validación estricta: algoritmo fijo (sin "alg confusion"), claims obligatorios, issuer,
  audience, tipo y tolerancia de reloj acotada.
"""

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import decode_pem, settings
from app.core.exceptions import AuthenticationError

ACCESS_TOKEN_TYPE = "at+jwt"  # noqa: S105 - tipo de token (RFC 9068), no es un secreto
REQUIRED_CLAIMS = ["exp", "iat", "nbf", "sub", "sid", "jti", "iss", "aud"]


@dataclass(frozen=True)
class VerificationKey:
    """Llave que solo valida tokens (p. ej. una anterior publicada solo como pública)."""

    kid: str
    public: Any
    jwk: dict[str, str] | None


@dataclass(frozen=True)
class SigningKey(VerificationKey):
    """Llave que además firma: la parte privada existe siempre (lo garantiza el tipo)."""

    private: Any


@dataclass(frozen=True)
class AccessToken:
    token: str
    jti: str
    expires_at: datetime
    expires_in: int


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _ec_jwk(public_key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    numbers = public_key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }


def _thumbprint(jwk: dict[str, str]) -> str:
    """RFC 7638: SHA-256 de los miembros requeridos en orden lexicográfico."""
    canonical = json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"))
    return _b64url(hashlib.sha256(canonical.encode()).digest())[:16]


def _load_ec_key(pem: str) -> VerificationKey:
    """Llave EC P-256 en PEM: privada (firma y valida) o pública (solo valida)."""
    data = pem.encode()
    private = None
    if "PRIVATE KEY" in pem:
        private = serialization.load_pem_private_key(data, password=None)
        public = private.public_key()
    else:
        public = serialization.load_pem_public_key(data)
    if not isinstance(public, ec.EllipticCurvePublicKey) or public.curve.name != "secp256r1":
        raise ValueError("Las llaves JWT ES256 deben ser EC P-256")
    jwk = _ec_jwk(public)
    kid = _thumbprint(jwk)
    jwk = {**jwk, "kid": kid, "use": "sig", "alg": "ES256"}
    if private is None:
        return VerificationKey(kid=kid, public=public, jwk=jwk)
    return SigningKey(kid=kid, public=public, jwk=jwk, private=private)


@lru_cache
def _keys() -> tuple[SigningKey, dict[str, VerificationKey]]:
    """(llave de firma actual, todas las llaves de verificación por kid)."""
    if settings.JWT_ALGORITHM == "HS256":
        secret = settings.JWT_SECRET_KEY
        kid = hashlib.sha256(secret.encode()).hexdigest()[:16]
        key = SigningKey(kid=kid, public=secret, jwk=None, private=secret)
        return key, {kid: key}
    current = _load_ec_key(settings.JWT_PRIVATE_KEY)
    if not isinstance(current, SigningKey):
        # Se detecta al cargar las llaves (arranque o primera petición), no al emitir el primer token:
        # con solo la pública nadie podría iniciar sesión y la causa quedaría escondida.
        raise ValueError("JWT_PRIVATE_KEY debe ser la llave PRIVADA EC P-256 (se recibió una pública)")
    verification: dict[str, VerificationKey] = {current.kid: current}
    for pem in filter(None, (decode_pem(p) for p in settings.JWT_PREVIOUS_KEYS.split("||"))):
        previous = _load_ec_key(pem)
        verification.setdefault(previous.kid, previous)
    return current, verification


def jwks() -> dict[str, list[dict[str, str]]]:
    """Llaves públicas vigentes (JWKS). Vacío con HS256: el secreto nunca se publica."""
    _, keys = _keys()
    return {"keys": [k.jwk for k in keys.values() if k.jwk]}


def create_access_token(
    *,
    user_id: int,
    role: str,
    session_id: str,
    not_after: datetime | None = None,
    issued_at: datetime | None = None,
) -> AccessToken:
    """`not_after`: vencimiento de la sesión; el token nunca dura más que ella.

    `issued_at`: el mismo instante con que se creó la sesión (al iniciar sesión el token dura
    exactamente lo mismo que ella).
    """
    current, _ = _keys()
    now = issued_at or datetime.now(UTC)
    expires_delta = timedelta(minutes=settings.JWT_ACCESS_TTL_MINUTES)
    if not_after is not None:
        expires_delta = max(timedelta(seconds=1), min(expires_delta, not_after - now))
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "sub": str(user_id),
        "sid": session_id,
        "jti": jti,
        "role": role,
        "iat": now,
        "nbf": now,
        "exp": now + expires_delta,
    }
    token = jwt.encode(
        payload,
        current.private,
        algorithm=settings.JWT_ALGORITHM,
        headers={"kid": current.kid, "typ": ACCESS_TOKEN_TYPE},
    )
    return AccessToken(
        token=token, jti=jti, expires_at=now + expires_delta, expires_in=int(expires_delta.total_seconds())
    )


def decode_access_token(token: str) -> dict[str, Any]:
    _, keys = _keys()
    try:
        header = jwt.get_unverified_header(token)
        key = keys.get(header.get("kid", ""))
        if key is None or header.get("typ") != ACCESS_TOKEN_TYPE:
            raise AuthenticationError("Token inválido", code="TOKEN_INVALID")
        return jwt.decode(
            token,
            key.public,
            algorithms=[settings.JWT_ALGORITHM],  # algoritmo fijo: nunca el del token
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            leeway=settings.JWT_LEEWAY_SECONDS,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("La sesión ha expirado", code="TOKEN_EXPIRED") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Token inválido", code="TOKEN_INVALID") from exc
