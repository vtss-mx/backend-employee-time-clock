"""Cifrado simétrico (Fernet) y hashing de tokens.

- Los embeddings faciales y los tokens QR se guardan cifrados en la base de datos.
- Los tokens QR se buscan mediante su hash SHA-256 (son aleatorios de 256 bits,
  por lo que un hash rápido es suficiente y permite búsqueda indexada).
"""

import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_fernet = Fernet(settings.DATA_ENCRYPTION_KEY.encode())


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet.encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    try:
        return _fernet.decrypt(token)
    except InvalidToken as exc:
        raise ValueError("No fue posible descifrar el dato (¿cambió DATA_ENCRYPTION_KEY?)") from exc


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
