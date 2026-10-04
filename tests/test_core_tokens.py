"""Llaves de firma JWT (app/core/tokens.py): rotación, HS256 y configuraciones inválidas.

Las llaves se cargan una vez por proceso (`_keys` con lru_cache); cada prueba cambia la
configuración, limpia esa caché y la vuelve a limpiar al final para no afectar a las demás.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core import tokens
from app.core.config import settings
from app.core.exceptions import AuthenticationError


def _pem(key: ec.EllipticCurvePrivateKey, *, public: bool = False) -> str:
    if public:
        return (
            key.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def keys_config(monkeypatch):
    """Cambia la configuración de llaves y recarga `_keys` (antes y después de la prueba)."""

    def apply(**values: str) -> None:
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)
        tokens._keys.cache_clear()

    yield apply
    monkeypatch.undo()
    tokens._keys.cache_clear()


def _issue(**extra) -> str:
    return tokens.create_access_token(user_id=7, role="COMPANY", session_id="sesion-1", **extra).token


def test_previous_keys_keep_validating_tokens_after_rotation(keys_config):
    """Rotar la llave no cierra sesiones: los tokens firmados con la anterior siguen siendo válidos
    (llave anterior privada o solo pública) y el JWKS publica todas, sin repetir y sin la parte privada."""
    # Dos llaves retiradas: una se conserva completa y de la otra solo se publica la parte pública.
    kept, public_only = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    keys_config(JWT_PRIVATE_KEY=_pem(kept))
    signed_with_kept = _issue()
    keys_config(JWT_PRIVATE_KEY=_pem(public_only))
    signed_with_public_only = _issue()

    current = ec.generate_private_key(ec.SECP256R1())
    previous = "||".join([_pem(kept), _pem(public_only, public=True), _pem(current)])  # la vigente repetida
    keys_config(JWT_PRIVATE_KEY=_pem(current), JWT_PREVIOUS_KEYS=previous)

    assert tokens.decode_access_token(signed_with_kept)["sub"] == "7"
    assert tokens.decode_access_token(signed_with_public_only)["sid"] == "sesion-1"
    published = tokens.jwks()["keys"]
    assert len(published) == 3 and len({key["kid"] for key in published}) == 3
    assert all("d" not in key for key in published)  # nunca la parte privada
    new_token = _issue()
    assert jwt.get_unverified_header(new_token)["kid"] == published[0]["kid"]  # firma la vigente


def test_hs256_signs_with_the_secret_and_publishes_no_keys(keys_config):
    keys_config(JWT_ALGORITHM="HS256", JWT_SECRET_KEY="s" * 40)
    token = _issue()
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    assert tokens.decode_access_token(token)["role"] == "COMPANY"
    assert tokens.jwks() == {"keys": []}  # el secreto jamás se publica


@pytest.mark.parametrize(
    ("make_key", "message"),
    [
        (lambda: _pem(ec.generate_private_key(ec.SECP256R1()), public=True), "PRIVADA"),
        (lambda: _pem(ec.generate_private_key(ec.SECP384R1())), "P-256"),
    ],
    ids=["solo-publica", "otra-curva"],
)
def test_an_unusable_signing_key_fails_when_loading_not_when_signing(keys_config, make_key, message):
    """Con una llave pública (o de otra curva) nadie podría iniciar sesión: el error es claro y al
    cargar las llaves, no un 500 escondido al emitir el primer token."""
    keys_config(JWT_PRIVATE_KEY=make_key())
    with pytest.raises(ValueError, match=message):
        tokens.jwks()


def test_token_without_session_end_lasts_the_configured_ttl():
    issued = tokens.create_access_token(user_id=1, role="ADMIN", session_id="s")
    assert issued.expires_in == settings.JWT_ACCESS_TTL_MINUTES * 60


def test_session_end_caps_the_token_but_never_below_one_second():
    now = datetime.now(UTC)
    short = tokens.create_access_token(user_id=1, role="ADMIN", session_id="s", not_after=now, issued_at=now)
    assert short.expires_in == 1


@pytest.mark.parametrize(
    "headers",
    [{"kid": "llave-desconocida", "typ": tokens.ACCESS_TOKEN_TYPE}, {"typ": "JWT"}],
    ids=["kid-desconocido", "otro-tipo"],
)
def test_tokens_with_unknown_key_or_type_are_rejected(headers):
    """Un token con un `kid` que no es nuestro o que no es de acceso (p. ej. un id token) no se
    valida contra ninguna llave."""
    current, _ = tokens._keys()
    claims = {"sub": "1", "exp": datetime.now(UTC) + timedelta(hours=1)}
    forged = jwt.encode(claims, current.private, algorithm="ES256", headers={"kid": current.kid, **headers})
    with pytest.raises(AuthenticationError) as exc:
        tokens.decode_access_token(forged)
    assert exc.value.code == "TOKEN_INVALID"
