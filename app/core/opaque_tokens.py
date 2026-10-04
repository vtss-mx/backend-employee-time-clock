"""Tokens opacos `<id>.<secreto>` para cookies HttpOnly (refresh token, cuenta recordada).

En la BD solo se guarda el SHA-256 del secreto (`hash_token`); la comparación es en tiempo constante.
"""

import base64
import hashlib
import hmac
import secrets
import uuid

from app.core.config import settings
from app.core.crypto import constant_time_equals, hash_token

#: Llave para derivar el siguiente secreto de un refresh token (separada de la de cifrado por su
#: etiqueta): sin ella, conocer el secreto anterior no permite calcular el siguiente.
_ROTATION_KEY = hmac.new(settings.DATA_ENCRYPTION_KEY.encode(), b"refresh-rotation", hashlib.sha256).digest()


def new_id() -> str:
    return uuid.uuid4().hex


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def next_secret(token_id: str, secret: str) -> str:
    """El secreto que sigue a `secret` en la rotación: SIEMPRE el mismo para el mismo anterior. Si la
    respuesta que lo entregó se perdió y el cliente reintenta con el anterior (dentro de la gracia),
    recibe exactamente el mismo: el reintento es idempotente y no termina en un falso "token
    reutilizado" (que cerraría la sesión). Imposible de adivinar sin la llave del servidor."""
    digest = hmac.new(_ROTATION_KEY, f"{token_id}.{secret}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def secret_matches(secret: str, *hashes: str | None) -> bool:
    """¿El secreto corresponde a alguno de los hashes guardados?"""
    presented = hash_token(secret)
    return any(stored is not None and constant_time_equals(presented, stored) for stored in hashes)


def split_token(token: str | None) -> tuple[str, str]:
    """`<id>.<secreto>` → (id, secreto); vacíos si el token no tiene esa forma."""
    token_id, _, secret = (token or "").partition(".")
    return (token_id, secret) if token_id and secret else ("", "")
