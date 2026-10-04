"""Hashing de contraseñas (Argon2id) con concurrencia acotada. Los JWT están en `app.core.tokens`."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError
from app.core.system import available_cpus

_password_hasher = PasswordHasher()

# Argon2id es costoso a propósito (~64 MB y decenas de ms por hash). Sin límite, una ráfaga de
# miles de inicios de sesión agotaría la memoria y la CPU. Se calculan a lo sumo N a la vez por
# SERVIDOR (los núcleos repartidos entre los API_WORKERS procesos, como el motor facial); el resto
# espera en orden y, si se excede la espera, recibe 503 reintentable.
_hash_slots = threading.BoundedSemaphore(
    settings.PASSWORD_HASH_CONCURRENCY or max(1, available_cpus() // max(1, settings.API_WORKERS))
)


@contextmanager
def _hash_slot() -> Iterator[None]:
    if not _hash_slots.acquire(timeout=settings.PASSWORD_HASH_WAIT_SECONDS):
        raise ServiceUnavailableError(
            "Hay muchos inicios de sesión en este momento. Intenta nuevamente en unos segundos.",
            code="AUTH_BUSY",
            retry_after=2,
        )
    try:
        yield
    finally:
        _hash_slots.release()


# Hash ficticio para igualar el tiempo de respuesta cuando el usuario no existe
# (evita enumerar correos por diferencia de tiempos).
_DUMMY_HASH = _password_hasher.hash("timing-attack-dummy-password")


def hash_password(password: str) -> str:
    with _hash_slot():
        return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    with _hash_slot():
        try:
            return _password_hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
        except VerificationError, InvalidHashError:
            return False


def password_needs_rehash(password_hash: str) -> bool:
    return _password_hasher.check_needs_rehash(password_hash)
