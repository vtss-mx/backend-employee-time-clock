"""Un autenticador WebAuthn de mentira para las pruebas de las llaves de acceso: crea credenciales (attestation
`none`, como las llaves sincronizadas de Apple y Google) y firma retos con una llave ECDSA P-256, en el formato JSON que
devuelve `navigator.credentials` en el navegador. Lo que el servidor verifica con py_webauthn se arma aquí byte por byte
(RFC de WebAuthn nivel 2: clientDataJSON, authenticatorData y attestationObject en CBOR)."""

import hashlib
import json
import secrets
from dataclasses import dataclass, field

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from app.core.config import settings

#: Banderas de `authenticatorData`: presencia (UP), verificación (UV), credencial adjunta (AT), respaldable (BE) y
#: respaldada (BS).
UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40


def _client_data(kind: str, challenge: str, origin: str) -> bytes:
    return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()


def _cose_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    numbers = key.public_key().public_numbers()
    return cbor2.dumps(
        {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}  # EC2, ES256, P-256
    )


@dataclass
class FakeAuthenticator:
    """Una llave de acceso en un dispositivo: su llave privada, el id de su credencial y su contador de firmas."""

    rp_id: str = field(default_factory=lambda: settings.WEBAUTHN_RP_ID)
    origin: str = field(default_factory=lambda: settings.webauthn_origins[0])
    key: ec.EllipticCurvePrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    credential_id: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    sign_count: int = 0
    #: Una llave sincronizada (BE+BS) manda siempre 0 en el contador; una de un solo dispositivo lo incrementa.
    synced: bool = True
    user_verified: bool = True

    @property
    def credential_id_b64(self) -> str:
        return bytes_to_base64url(self.credential_id)

    def _flags(self, *, attested: bool) -> int:
        flags = UP | (UV if self.user_verified else 0) | (AT if attested else 0)
        return flags | (BE | BS if self.synced else 0)

    def _count(self) -> int:
        if self.synced:
            return 0
        self.sign_count += 1
        return self.sign_count

    def register(self, options: dict, *, origin: str | None = None, transports: list[str] | None = None) -> dict:
        """La respuesta de `navigator.credentials.create` para las opciones que dio el servidor."""
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        attested = bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + _cose_key(self.key)
        auth_data = rp_hash + bytes([self._flags(attested=True)]) + self._count().to_bytes(4, "big") + attested
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": self.credential_id_b64,
            "rawId": self.credential_id_b64,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(
                    _client_data("webauthn.create", options["challenge"], origin or self.origin)
                ),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal"] if transports is None else transports,
            },
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
        }

    def sign(self, options: dict, *, origin: str | None = None, count: int | None = None) -> dict:
        """La respuesta de `navigator.credentials.get`: la firma del reto (y de `authenticatorData`) con la llave."""
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        sign_count = self._count() if count is None else count
        auth_data = rp_hash + bytes([self._flags(attested=False)]) + sign_count.to_bytes(4, "big")
        client_data = _client_data("webauthn.get", options["challenge"], origin or self.origin)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": self.credential_id_b64,
            "rawId": self.credential_id_b64,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(b"1"),
            },
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
        }


def challenge_bytes(options: dict) -> bytes:
    return base64url_to_bytes(options["challenge"])


def register(client, headers: dict[str, str], authenticator: FakeAuthenticator, name: str = "Mi teléfono") -> dict:
    """Registra una llave en la cuenta de `headers` con la ceremonia completa; devuelve la respuesta del servidor."""
    options = client.post("/api/auth/passkeys/options", headers=headers).json()["data"]
    body = {"token": options["token"], "name": name, "credential": authenticator.register(options["options"])}
    return client.post("/api/auth/passkeys", json=body, headers=headers)


def login_with(client, authenticator: FakeAuthenticator, **extra) -> dict:
    """Entra con la llave: pide el reto, lo firma y lo manda (con `remember`, `location` o `device` si se piden)."""
    options = client.post("/api/auth/login/passkey/options")
    assert options.status_code == 200, options.text
    body = {
        "token": options.json()["data"]["token"],
        "credential": authenticator.sign(options.json()["data"]["options"]),
        **extra,
    }
    return client.post("/api/auth/login/passkey", json=body)
