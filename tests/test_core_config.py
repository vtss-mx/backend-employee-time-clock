"""Configuración (app/core/config.py) y recursos del sistema (app/core/system.py).

Un error en `.env` debe detenerse al arrancar con un mensaje que diga qué corregir, nunca convertirse
en un comportamiento distinto en silencio (otra base, otra llave, sin límites).
"""

import base64
import io

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core import system
from app.core.config import Settings, decode_pem


def _settings(**values) -> Settings:
    """Settings con estos valores sobre el entorno de pruebas (sin leer el `.env` del proyecto)."""
    return Settings(_env_file=None, **values)


def test_lists_are_read_from_comma_separated_text():
    config = _settings(CORS_ORIGINS=" https://a.mx , ,https://b.mx", FACE_BLOCKED_CAMERAS="OBS Virtual, ,ManyCam ")
    assert config.CORS_ORIGINS == ["https://a.mx", "https://b.mx"]
    assert config.FACE_BLOCKED_CAMERAS == ["obs virtual", "manycam"]  # se comparan en minúsculas
    accented = _settings(FACE_BLOCKED_CAMERAS="Câmera Virtual, Caméra virtuelle")
    assert accented.FACE_BLOCKED_CAMERAS == ["câmera virtual", "caméra virtuelle"]  # se pliegan al comparar (D-C4)
    assert _settings(CORS_ORIGINS=["https://c.mx"]).CORS_ORIGINS == ["https://c.mx"]  # una lista llega igual


@pytest.mark.parametrize("scheme", ["postgres://", "postgresql://"])
def test_database_url_always_uses_the_psycopg_driver(scheme):
    url = _settings(DATABASE_URL=f"{scheme}u:p@db:5432/tc").DATABASE_URL
    assert url == "postgresql+psycopg://u:p@db:5432/tc"


def test_database_url_is_built_from_postgres_variables_with_escaped_password():
    config = _settings(
        DATABASE_URL="", POSTGRES_PASSWORD="p@ss:/word", POSTGRES_HOST="db", POSTGRES_USER="tc", DB_APP_PASSWORD=""
    )
    assert config.DATABASE_URL == "postgresql+psycopg://tc:p%40ss%3A/word@db:5432/timeclock"


def test_direct_url_is_the_same_database_unless_a_direct_host_is_given():
    """Sin PgBouncer las migraciones usan la misma URL; con él, la conexión directa a PostgreSQL."""
    base = {
        "DATABASE_URL": "",
        "DATABASE_DIRECT_URL": "",
        "POSTGRES_PASSWORD": "pw",
        "POSTGRES_USER": "tc",
        "DB_APP_PASSWORD": "",
    }
    alone = _settings(**base, POSTGRES_HOST="db")
    assert alone.DATABASE_DIRECT_URL == alone.DATABASE_URL == "postgresql+psycopg://tc:pw@db:5432/timeclock"
    pooled = _settings(
        **base, POSTGRES_HOST="pgbouncer", POSTGRES_PORT=6432, DB_POOLER="pgbouncer", POSTGRES_DIRECT_HOST="db"
    )
    assert pooled.DATABASE_URL == "postgresql+psycopg://tc:pw@pgbouncer:6432/timeclock"
    assert pooled.DATABASE_DIRECT_URL == "postgresql+psycopg://tc:pw@db:5432/timeclock"  # el puerto de PostgreSQL
    explicit = _settings(DATABASE_URL="postgres://u:p@pool:6432/tc", DATABASE_DIRECT_URL="postgres://u:p@pg:5432/tc")
    assert explicit.DATABASE_DIRECT_URL == "postgresql+psycopg://u:p@pg:5432/tc"  # mismo driver en las dos


def test_api_workers_zero_means_automatic(monkeypatch):
    monkeypatch.setattr("app.core.config.default_api_workers", lambda: 3)
    assert _settings(API_WORKERS=0).API_WORKERS == 3
    assert _settings(API_WORKERS=2).API_WORKERS == 2


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"PAGE_SIZE_DEFAULT": 60, "PAGE_SIZE_MAX": 50}, "PAGE_SIZE_DEFAULT"),
        ({"DATABASE_URL": "", "POSTGRES_PASSWORD": "", "DB_APP_PASSWORD": ""}, "POSTGRES_PASSWORD"),
        ({"JWT_ALGORITHM": "ES256", "JWT_PRIVATE_KEY": ""}, "JWT_PRIVATE_KEY"),
        ({"JWT_ALGORITHM": "HS256", "JWT_SECRET_KEY": "corto"}, "JWT_SECRET_KEY"),
        ({"DATA_ENCRYPTION_KEY": "no-es-fernet"}, "Fernet"),
        ({"DATA_ENCRYPTION_PREVIOUS_KEYS": "x, y"}, "Fernet"),
        # Las migraciones a través de PgBouncer romperían su candado de sesión y CREATE INDEX CONCURRENTLY.
        ({"DB_POOLER": "pgbouncer", "DATABASE_DIRECT_URL": ""}, "POSTGRES_DIRECT_HOST"),
        # Los límites de peticiones en Redis necesitan un Redis; el prefijo de llaves es una sola palabra.
        ({"RATE_LIMIT_BACKEND": "redis", "REDIS_HOST": "", "REDIS_URL": ""}, "REDIS_HOST"),
        ({"REDIS_KEY_PREFIX": "Mal Prefijo"}, "REDIS_KEY_PREFIX"),
        # Con varias réplicas cada una contaría por su lado: el límite real se multiplicaría.
        ({"ENVIRONMENT": "production", "RATE_LIMIT_BACKEND": "memory"}, "RATE_LIMIT_BACKEND"),
    ],
    ids=[
        "pagina",
        "sin-base",
        "es256-sin-llave",
        "hs256-corto",
        "fernet",
        "fernet-anterior",
        "pgbouncer-sin-directa",
        "redis-sin-host",
        "redis-prefijo",
        "limite-por-proceso-en-produccion",
    ],
)
def test_invalid_configuration_stops_the_start_with_a_clear_message(values, message):
    with pytest.raises(ValueError, match=message):
        _settings(**values)


def _private_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def test_pem_is_accepted_escaped_quoted_or_in_base64():
    """Cada plataforma guarda los secretos multilínea distinto (.env, Kubernetes, CI)."""
    pem = _private_pem()
    escaped = '"' + pem.replace("\n", "\\n") + '"'
    encoded = base64.b64encode(pem.encode()).decode()
    assert decode_pem(escaped).strip() == pem.strip()
    assert decode_pem(encoded) == pem
    assert decode_pem("  ''  ") == ""
    assert decode_pem("no-es-base64!") == "no-es-base64!"  # se deja tal cual: la validación lo rechaza
    assert pem == _settings(JWT_PRIVATE_KEY=encoded).JWT_PRIVATE_KEY


# ---------------------------------------------------------------- recursos del sistema


class _Os:
    """`os` sin afinidad de CPU (macOS, Windows): se usa el total de núcleos."""

    def __init__(self, cpus: int | None) -> None:
        self.cpus = cpus

    def cpu_count(self) -> int | None:
        return self.cpus


def _cgroup(monkeypatch, content: str | None) -> None:
    """Simula /sys/fs/cgroup/cpu.max (None = no existe, p. ej. fuera de un contenedor)."""

    def fake_open(path, *_args, **_kwargs):
        if content is None:
            raise FileNotFoundError(path)
        return io.StringIO(content)

    monkeypatch.setattr(system, "open", fake_open, raising=False)


@pytest.mark.parametrize(
    ("cpus", "cgroup", "expected"),
    [
        (8, None, 8),  # sin contenedor: todos los núcleos
        (8, "max 100000", 8),  # contenedor sin cuota
        (8, "200000 100000", 2),  # docker --cpus=2
        (8, "50000 100000", 1),  # --cpus=0.5: nunca menos de uno
        (8, "basura", 8),  # archivo ilegible: se ignora
        (None, None, 1),  # el sistema no sabe cuántos hay
    ],
)
def test_available_cpus_respects_the_container_quota(monkeypatch, cpus, cgroup, expected):
    monkeypatch.setattr(system, "os", _Os(cpus))
    _cgroup(monkeypatch, cgroup)
    assert system.available_cpus() == expected


@pytest.mark.parametrize(("cpus", "workers"), [(1, 1), (6, 3), (32, 4)])
def test_default_api_workers_leaves_half_the_cores_for_face_recognition(monkeypatch, cpus, workers):
    monkeypatch.setattr(system, "available_cpus", lambda: cpus)
    assert system.default_api_workers() == workers


_ROUTES = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t010015AC\t0003\t0\t0\t0\t00000000\t0\t0\t0
eth0\t000015AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
eth1\t0000A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
eth2\t000015AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
lo\t0000007F\t00000000\t0001\t0\t0\t0\t000000FF\t0\t0\t0
incompleta
"""


def test_trusted_proxies_are_this_host_and_its_directly_connected_networks(tmp_path):
    """FORWARDED_ALLOW_IPS=auto: solo quien está en la red del contenedor (el gateway) dice la IP del cliente.
    Sin la ruta por defecto (sería confiar en todo Internet), sin `lo`, sin repetir y sin líneas incompletas."""
    routes = tmp_path / "route"
    routes.write_text(_ROUTES)
    assert system.local_networks(str(routes)) == ["172.21.0.0/16", "192.168.0.0/24"]
    assert system.trusted_proxies(str(routes)) == "127.0.0.1,172.21.0.0/16,192.168.0.0/24"


def test_without_routes_only_this_host_is_trusted(tmp_path):
    """Fuera de Linux (o sin /proc) no se adivina: solo el mismo equipo."""
    assert system.trusted_proxies(str(tmp_path / "no-existe")) == "127.0.0.1"
