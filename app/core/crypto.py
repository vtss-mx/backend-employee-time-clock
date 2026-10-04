"""Cifrado simétrico (Fernet) y hashing de tokens.

- Los embeddings faciales y los tokens QR se guardan cifrados en la base de datos.
- Los tokens QR se buscan mediante su hash SHA-256 (son aleatorios de 256 bits,
  por lo que un hash rápido es suficiente y permite búsqueda indexada).
"""

import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import settings

#: Cifra con la llave vigente y descifra con ella o con las anteriores (DATA_ENCRYPTION_PREVIOUS_KEYS):
#: rotar la llave no deja ilegibles los datos ya guardados.
_fernet = MultiFernet([Fernet(key.encode()) for key in settings.data_encryption_keys])


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet.encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    try:
        return _fernet.decrypt(token)
    except InvalidToken as exc:
        raise ValueError("No fue posible descifrar el dato (¿cambió DATA_ENCRYPTION_KEY?)") from exc


def try_decrypt(token: bytes) -> bytes | None:
    """Como `decrypt_bytes`, pero None si el dato está dañado o es de una llave que ya no se tiene:
    quien lo usa lo omite (y lo registra) en lugar de tumbar la operación completa."""
    try:
        return _fernet.decrypt(token)
    except InvalidToken:
        return None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
