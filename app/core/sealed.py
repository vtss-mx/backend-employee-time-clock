"""Tokens SELLADOS (cifrados y firmados) que viajan por el cliente en lugar de guardar estado en el servidor.

Con N réplicas detrás de PgBouncer, un estado de corta vida que solo necesita una petición y la siguiente (los colores
del destello dictado, la sesión de preguntas por voz del registro facial) no vive en memoria ni en la base: va en un
token Fernet que el cliente devuelve tal cual. Nadie puede leerlo ni alterarlo sin la llave; una llave anterior
(`DATA_ENCRYPTION_PREVIOUS_KEYS`) sigue abriendo lo que selló, así rotarla no rompe lo que está en curso.

Cada uso deriva su propia llave con una etiqueta (`purpose`): un token de un uso nunca se acepta en otro.
"""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, MultiFernet

from app.core.config import settings


def derive_fernet(secret: str, purpose: bytes) -> Fernet:
    """La llave Fernet de un uso, derivada de una llave de la plataforma con HMAC-SHA256 sobre su etiqueta."""
    digest = hmac.new(secret.encode(), purpose, hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def sealer(purpose: bytes) -> MultiFernet:
    """La llave vigente sella; las anteriores solo abren."""
    return MultiFernet([derive_fernet(key, purpose) for key in settings.data_encryption_keys])
