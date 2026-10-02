"""Genera el `backend/.env` inicial de una instalación nueva (no depende de la configuración de la app).

python3 backend/scripts/generate_secrets.py > backend/.env     # desde la raíz del proyecto

Imprime los secretos y la base de datos que comparten la API y el contenedor de PostgreSQL; todo
lo demás tiene valor por defecto en el código y se agrega a `backend/.env` solo si se quiere cambiar.
"""

import base64
import secrets

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def es256_private_key_b64() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return base64.b64encode(pem).decode()


def main() -> None:
    print(f"JWT_PRIVATE_KEY={es256_private_key_b64()}")
    print(f"DATA_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
    print("POSTGRES_DB=timeclock")
    print("POSTGRES_USER=timeclock")
    print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}")


if __name__ == "__main__":
    main()
