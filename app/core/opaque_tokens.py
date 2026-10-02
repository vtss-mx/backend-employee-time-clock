"""Tokens opacos `<id>.<secreto>` para cookies HttpOnly (refresh token, cuenta recordada).

En la BD solo se guarda el SHA-256 del secreto (`hash_token`); la comparación es en tiempo constante.
"""

import secrets
import uuid

from app.core.crypto import constant_time_equals, hash_token


def new_id() -> str:
    return uuid.uuid4().hex


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def secret_matches(secret: str, *hashes: str | None) -> bool:
    """¿El secreto corresponde a alguno de los hashes guardados?"""
    presented = hash_token(secret)
    return any(stored is not None and constant_time_equals(presented, stored) for stored in hashes)


def split_token(token: str | None) -> tuple[str, str]:
    """`<id>.<secreto>` → (id, secreto); vacíos si el token no tiene esa forma."""
    token_id, _, secret = (token or "").partition(".")
    return (token_id, secret) if token_id and secret else ("", "")
