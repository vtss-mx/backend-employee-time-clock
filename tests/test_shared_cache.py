"""Caché compartida entre réplicas (Redis; app/core/cache.py) y quién la usa: los catálogos en dos niveles
(catalog_service) y los límites de peticiones compartidos (rate_limit.RedisRateLimiter).

Todo con el Redis falso de `tests/redis_support.py` (sin red): acierto, fallo y vencimiento, Redis caído (el
cortacircuitos, su reintento y que se registre UNA vez), un valor ilegible, el prefijo, la limpieza al migrar y dos
"réplicas" (dos cachés locales sobre el mismo Redis) que ven el cambio tras `clear`.
"""

import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from redis import BlockingConnectionPool
from sqlalchemy.exc import OperationalError

from app.core import cache as cache_module
from app.core.cache import SharedCache, build_client, decode, encode, load_cache, shared_cache, use_cache
from app.core.config import Settings, settings
from app.middleware import rate_limit
from app.middleware.rate_limit import DatabaseRateLimiter, InMemoryRateLimiter, RedisRateLimiter, subject_key
from app.models import CatalogRole
from app.services import catalog_service
from app.services.catalog_service import CatalogData, catalog_key, catalog_version, clear_catalog_cache
from tests.conftest import SessionLocal
from tests.redis_support import FakeRedis, shared
from tests.test_performance import count_queries


@pytest.fixture
def server() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def redis_cache(server):
    """La caché del proceso apunta al Redis falso; al terminar vuelve a la configuración (apagada en las pruebas)."""
    cache = shared(server)
    use_cache(cache)
    yield cache
    use_cache(None)


def _clock(monkeypatch) -> list[float]:
    """Reloj monótono controlado por la prueba (el cortacircuitos no espera de verdad)."""
    now = [100.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: now[0])
    return now


# ---------------------------------------------------------------- la capa


def test_values_travel_as_json_compressed_or_not():
    value = {"rows": [{"code": f"C{i}", "name": "Nombre repetido " * 3} for i in range(50)], "é": None}
    raw, packed = encode(value, 0), encode(value, 1)
    assert raw[:1] == b"{" and packed[:1] == b"\x78"  # un JSON nunca empieza con la cabecera de zlib
    assert len(packed) < len(raw) / 5
    assert decode(raw) == decode(packed) == value


def test_a_disabled_cache_answers_nothing_and_touches_nothing(caplog):
    cache = SharedCache(None, prefix="x", retry_seconds=1)
    assert not cache.enabled and not cache.available
    assert cache.get_json("k") is None and cache.set_json("k", 1, 10) is False
    assert cache.delete("k") is False and cache.delete_prefix("*") is None and cache.hit_window("k", 60) is None
    assert cache.describe() == {"backend": "disabled", "target": None, "status": "disabled", "failures": 0}
    cache.close()
    assert caplog.records == []


def test_hit_miss_and_expiry_are_shared_between_two_replicas(server):
    first, second = shared(server), shared(server)
    assert first.get_json("k") is None  # fallo
    assert first.set_json("k", {"a": [1, "é"]}, 10) is True
    assert second.get_json("k") == {"a": [1, "é"]}  # acierto desde la otra réplica
    assert second.describe()["status"] == "ok" and second.available
    server.now += 11  # venció
    assert first.get_json("k") is None
    assert first.set_json("k", 1, 0.2) and server.ttl(first._key("k")) == 1  # vigencia mínima de 1 s


def test_an_unreadable_value_is_discarded_and_recomputed(server, caplog):
    cache = shared(server)
    for garbage in (b"\x78\x9c no es zlib", b"{ni json", b"\xff\xfe"):
        server.store[cache._key("k")] = garbage
        with caplog.at_level(logging.WARNING):
            assert cache.get_json("k") is None
        assert cache._key("k") not in server.store  # se borró: quien lo pidió lo vuelve a escribir
    assert caplog.text.count("Valor ilegible en Redis (k)") == 3
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)  # se cura sola: no es una falla


def test_the_circuit_breaker_opens_once_is_logged_once_and_retries_after_the_pause(server, monkeypatch, caplog):
    now = _clock(monkeypatch)
    cache = shared(server, retry_seconds=5)
    server.down = True
    with caplog.at_level(logging.DEBUG):
        assert cache.get_json("k") is None  # la falla real: abre el circuito
        calls = len(server.calls)
        assert cache.set_json("k", 1, 10) is False and cache.hit_window("r", 60) is None
        assert len(server.calls) == calls  # circuito abierto: ni se intenta
        assert not cache.available and cache.describe() == {
            "backend": "redis",
            "target": "fake:6379/0",
            "status": "unavailable",
            "failures": 1,
        }
        now[0] += 5.1  # pasó la pausa: un intento más, que vuelve a fallar
        assert cache.get_json("k") is None and len(server.calls) == calls + 1
        assert cache.failures == 2
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "Redis (fake:6379/0) no responde en get: ConnectionError" in errors[0].message
    assert "sigue sin responder en get" in caplog.text  # la segunda, solo en el detalle del proceso
    server.down = False
    now[0] += 5.1
    caplog.clear()
    assert cache.set_json("k", 7, 10) is True and cache.get_json("k") == 7
    assert "volvió a responder" in caplog.text and cache.describe()["status"] == "ok"


def test_the_prefix_isolates_environments_sharing_one_redis(server):
    local, production = shared(server, prefix="local"), shared(server, prefix="production")
    local.set_json("catalogs:v1", "local", 60)
    production.set_json("catalogs:v1", "production", 60)
    assert local.get_json("catalogs:v1") == "local" and production.get_json("catalogs:v1") == "production"
    assert local.delete_prefix("catalogs:*") == 1
    assert local.get_json("catalogs:v1") is None and production.get_json("catalogs:v1") == "production"
    assert production.delete_prefix("nada:*") == 0


def test_the_window_counter_is_atomic_and_expires_once(server):
    cache = shared(server)
    assert [cache.hit_window("rl:k", 60) for _ in range(3)] == [1, 2, 3]
    assert server.ttl(cache._key("rl:k")) == 60
    server.now += 30
    assert cache.hit_window("rl:k", 60) == 4 and server.ttl(cache._key("rl:k")) == 30  # EXPIRE NX: no se alarga
    server.now += 31
    assert cache.hit_window("rl:k", 60) == 1  # ventana nueva


def test_closing_releases_the_pool_and_tolerates_a_failure(server, caplog):
    cache = shared(server)
    cache.close()
    assert server.closed
    server.fail_on_close = True
    with caplog.at_level(logging.DEBUG):
        cache.close()
    assert "Al cerrar Redis: ConnectionError" in caplog.text


def test_the_real_client_has_short_timeouts_a_blocking_pool_and_no_retries_of_its_own(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_CONNECT_TIMEOUT_MS", 150)
    monkeypatch.setattr(settings, "REDIS_TIMEOUT_MS", 250)
    monkeypatch.setattr(settings, "REDIS_MAX_CONNECTIONS", 3)
    client = build_client("redis://:secreto@redis:6379/2")
    pool = client.connection_pool
    kwargs = pool.connection_kwargs
    assert (kwargs["socket_connect_timeout"], kwargs["socket_timeout"]) == (0.15, 0.25)
    assert kwargs["db"] == 2 and kwargs["password"] == "secreto"
    # Pool bloqueante: con las 3 ocupadas espera hasta 250 ms una libre (un pico no abre el circuito; una caída sí).
    assert isinstance(pool, BlockingConnectionPool) and pool.max_connections == 3 and pool.timeout == 0.25
    assert client.get_retry()._retries == 0  # por omisión redis-py reintenta 10 veces con espera creciente
    client.close()


def test_the_cache_is_built_from_the_configuration_without_logging_the_password(monkeypatch, caplog):
    monkeypatch.setattr(settings, "REDIS_URL", "")
    monkeypatch.setattr(settings, "REDIS_HOST", "")
    with caplog.at_level(logging.INFO):
        disabled = load_cache()
    assert not disabled.enabled and "apagada (REDIS_HOST y REDIS_URL vacíos)" in caplog.text
    caplog.clear()
    monkeypatch.setattr(settings, "REDIS_HOST", "redis")
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "muy-secreto")
    monkeypatch.setattr(settings, "REDIS_KEY_PREFIX", "staging")
    with caplog.at_level(logging.INFO):
        enabled = load_cache()
    assert enabled.enabled and enabled.describe()["target"] == "redis:6379/0"
    assert "redis:6379/0, prefijo staging" in caplog.text and "muy-secreto" not in caplog.text
    enabled.close()


def test_the_process_cache_is_built_once_and_can_be_replaced(server):
    use_cache(None)
    first = shared_cache()
    assert shared_cache() is first and not first.enabled  # en las pruebas no hay Redis configurado
    fake = shared(server)
    use_cache(fake)
    assert shared_cache() is fake
    use_cache(None)


def test_redis_url_is_built_from_its_parts_and_the_target_never_has_the_password():
    def config(**values) -> Settings:
        return Settings(_env_file=None, **values)

    assert config(REDIS_URL="", REDIS_HOST="").redis_url == ""
    built = config(REDIS_URL="", REDIS_HOST="redis", REDIS_PASSWORD="p@ss/w:ord", REDIS_DB=3)
    assert built.redis_url == "redis://:p%40ss%2Fw%3Aord@redis:6379/3" and built.redis_target == "redis:6379/3"
    assert config(REDIS_URL="", REDIS_HOST="redis", REDIS_PASSWORD="").redis_url == "redis://redis:6379/0"
    explicit = config(REDIS_URL="rediss://:x@cache.example", REDIS_HOST="otro")
    assert explicit.redis_url == "rediss://:x@cache.example" and explicit.redis_target == "cache.example:6379/0"
    assert config(REDIS_KEY_PREFIX=" :timeclock: ").REDIS_KEY_PREFIX == "timeclock"


# ---------------------------------------------------------------- catálogos en dos niveles


def _fresh_replica() -> catalog_service._CatalogCache:
    """Otra réplica: su propia caché local, el mismo Redis."""
    return catalog_service._CatalogCache()


def test_catalogs_come_from_the_database_once_and_from_redis_in_the_other_replicas(redis_cache, server):
    clear_catalog_cache()
    first = _fresh_replica()
    with count_queries() as statements:
        loaded = first.get()
    assert len(statements) > 10 and "set" in server.calls  # la base, y la instantánea queda compartida
    assert list(server.store) == [redis_cache._key(catalog_key())]
    assert server.ttl(redis_cache._key(catalog_key())) == settings.CATALOG_REDIS_SECONDS
    second = _fresh_replica()
    with count_queries() as statements:
        from_redis = second.get()
    assert statements == []  # cero consultas: una lectura de Redis
    for locale in loaded:
        assert from_redis[locale].entries == loaded[locale].entries
        assert from_redis[locale].role_screens == loaded[locale].role_screens
        assert from_redis[locale].mode_methods == loaded[locale].mode_methods
        assert from_redis[locale].screen_modules == loaded[locale].screen_modules
    assert from_redis["en-US"].name("roles", "ADMIN") == loaded["en-US"].name("roles", "ADMIN") == "Admin"
    assert from_redis["es-MX"].name("roles", "ADMIN") == "Administrador"
    assert [row["code"] for row in from_redis["en-US"].entries["countries"][:2]] == ["MX", "US"]  # orden por idioma
    size_mb = len(server.store[redis_cache._key(catalog_key())]) / (1024 * 1024)
    assert size_mb < 0.5, f"{size_mb:.2f} MB"  # regla 17: la instantánea comprimida es pequeña


def test_a_change_is_seen_by_every_replica_after_clearing_the_shared_snapshot(redis_cache, server):
    clear_catalog_cache()
    first, second = _fresh_replica(), _fresh_replica()
    first.get()
    assert second.get()["es-MX"].name("roles", "ADMIN") == "Administrador"
    with SessionLocal() as db:
        db.get(CatalogRole, "ADMIN").name = "Superadministrador"
        db.commit()
    second.clear()  # solo venció su copia local: Redis sigue con la anterior
    assert second.get()["es-MX"].name("roles", "ADMIN") == "Administrador"
    assert catalog_service.clear_shared_catalogs() == 1  # lo que hace `migrate` al terminar
    first.clear()
    assert first.get()["es-MX"].name("roles", "ADMIN") == "Superadministrador"  # de la base, y la vuelve a compartir
    second.clear()
    with count_queries() as statements:
        assert second.get()["es-MX"].name("roles", "ADMIN") == "Superadministrador"
    assert statements == []


@pytest.mark.parametrize(
    "payload",
    [
        ["no", "es", "un", "diccionario"],
        {"entries": {}},
        {"entries": {}, "mode_methods": {}, "role_screens": {}, "screen_modules": {}, "translations": {}, "x": 1},
        {"entries": {}, "mode_methods": {}, "role_screens": {}, "screen_modules": {}, "translations": {}},
        {"entries": [], "mode_methods": {}, "role_screens": {}, "screen_modules": {}, "translations": {}},
    ],
)
def test_a_snapshot_with_another_shape_is_ignored_and_rewritten(redis_cache, server, payload):
    assert CatalogData.from_payload(payload) is None
    redis_cache.set_json(catalog_key(), payload, 600)
    with count_queries() as statements:
        assert _fresh_replica().get()["es-MX"].name("roles", "ADMIN")
    assert len(statements) > 10  # se recargó de la base
    assert CatalogData.from_payload(redis_cache.get_json(catalog_key())) is not None  # y se reescribió bien


def test_rows_without_a_code_are_not_a_valid_snapshot():
    good = CatalogData.from_payload(_fresh_replica().get() and _snapshot_payload())
    assert good is not None
    broken = {**good.to_payload(), "entries": {**good.entries, "roles": [{"name": "sin código"}]}}
    assert CatalogData.from_payload(broken) is None


def _snapshot_payload() -> dict:
    with SessionLocal() as db:
        return catalog_service.read_catalog_data(db).to_payload()


def test_the_snapshot_version_follows_the_models_the_locales_and_the_migrations(monkeypatch, tmp_path):
    version = catalog_version()
    assert len(version) == 16 and int(version, 16) >= 0 and catalog_key() == f"catalogs:{version}"
    monkeypatch.setattr(catalog_service, "MIGRATIONS_DIR", tmp_path)
    (tmp_path / "0999_otra.py").write_text("")
    catalog_version.cache_clear()
    try:
        assert catalog_version() != version  # otro conjunto de migraciones = otra llave
        assert catalog_version() == catalog_version()  # y se calcula una vez
    finally:
        catalog_version.cache_clear()
    monkeypatch.undo()
    catalog_version.cache_clear()
    assert catalog_version() == version


def test_a_zero_ttl_keeps_catalogs_out_of_redis(redis_cache, server, monkeypatch):
    monkeypatch.setattr(settings, "CATALOG_REDIS_SECONDS", 0)
    clear_catalog_cache()
    assert _fresh_replica().get()["es-MX"].name("roles", "ADMIN")
    assert server.store == {} and "get" not in server.calls


def test_redis_down_during_a_reload_falls_back_to_the_database(redis_cache, server, caplog):
    clear_catalog_cache()
    server.down = True
    with caplog.at_level(logging.ERROR):
        assert _fresh_replica().get()["en-US"].name("roles", "COMPANY") == "Company"
    assert sum("Redis (fake:6379/0) no responde" in r.message for r in caplog.records) == 1
    assert redis_cache.describe()["status"] == "unavailable"


def test_clearing_the_process_cache_also_clears_the_shared_snapshots(redis_cache, server):
    clear_catalog_cache()
    catalog_service.get_catalogs()
    assert len(server.store) == 1
    clear_catalog_cache()
    assert server.store == {}
    assert catalog_service.clear_shared_catalogs() == 0
    server.down = True
    assert catalog_service.clear_shared_catalogs() is None  # Redis caído: vencen solas


def _slow_reload(monkeypatch, *, fail: bool = False) -> tuple[threading.Event, threading.Event]:
    """La recarga de la base se detiene hasta que la prueba la suelta (y, si se pide, falla)."""
    entered, release = threading.Event(), threading.Event()
    real = catalog_service.read_catalog_data

    def slow(db):
        entered.set()
        assert release.wait(10)
        if fail:
            raise OperationalError("SELECT", {}, Exception("BD caída"))
        return real(db)

    monkeypatch.setattr(catalog_service, "read_catalog_data", slow)
    return entered, release


def test_a_cold_start_under_load_reloads_once_and_everyone_waits_for_it(monkeypatch):
    """Sin catálogos aún (una réplica que arranca con tráfico): una petición recarga y las demás esperan lo mismo,
    en lugar de recargar todas a la vez (medido en perf/scale: agotaban el pool de Redis y antes la base)."""
    entered, release = _slow_reload(monkeypatch)
    cache = catalog_service._CatalogCache()
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(cache.get())) for _ in range(8)]
    for thread in threads:
        thread.start()
    assert entered.wait(10)
    release.set()
    for thread in threads:
        thread.join(10)
    assert len(results) == 8 and all(result is results[0] for result in results)  # UNA recarga para las 8


def test_a_cold_start_that_fails_fails_for_everyone_waiting_without_retrying_in_line(monkeypatch):
    entered, release = _slow_reload(monkeypatch, fail=True)
    cache = catalog_service._CatalogCache()
    errors: list = []

    def attempt():
        try:
            cache.get()
        except OperationalError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    assert entered.wait(10)
    release.set()
    for thread in threads:
        thread.join(10)
    assert len(errors) == 4 and all(error is errors[0] for error in errors)  # el mismo error, un solo intento
    monkeypatch.undo()
    assert cache.get()["es-MX"].name("roles", "ADMIN")  # la siguiente petición vuelve a intentar y carga


def test_an_unexpected_failure_while_reloading_leaves_no_one_waiting(monkeypatch):
    cache = catalog_service._CatalogCache()

    def broken():
        raise RuntimeError("error de programación")

    monkeypatch.setattr(catalog_service, "_load_from_shared_or_database", broken)
    with pytest.raises(RuntimeError):
        cache.get()
    monkeypatch.undo()
    assert cache.get()["es-MX"].name("roles", "ADMIN")  # no quedó "recargando" para siempre


def test_a_waiter_that_times_out_keeps_waiting_while_the_reload_continues(monkeypatch):
    monkeypatch.setattr(settings, "REQUEST_QUEUE_TIMEOUT_SECONDS", 0.05)
    entered, release = _slow_reload(monkeypatch)
    cache = catalog_service._CatalogCache()
    results: list = []
    loader = threading.Thread(target=lambda: results.append(cache.get()))
    loader.start()
    assert entered.wait(10)
    waiter = threading.Thread(target=lambda: results.append(cache.get()))
    waiter.start()
    waiter.join(0.3)
    assert waiter.is_alive()  # pasó su tiempo de espera varias veces y sigue esperando: nunca recarga en paralelo
    release.set()
    loader.join(10)
    waiter.join(10)
    assert len(results) == 2 and results[0] is results[1]


# ---------------------------------------------------------------- límites de peticiones compartidos


def test_the_redis_limiter_is_shared_between_replicas_and_falls_back_per_process_when_redis_is_down(server):
    first, second = RedisRateLimiter(cache=shared(server)), RedisRateLimiter(cache=shared(server))
    assert [first.hit("k", 3, 60), second.hit("k", 3, 60), first.hit("k", 3, 60)] == [None, None, None]
    blocked = second.hit("k", 3, 60)  # el cuarto intento, aunque venga por otra réplica
    assert isinstance(blocked, int) and 1 <= blocked <= 60
    assert first.hit("otra", 3, 60) is None
    server.down = True  # cada réplica cuenta por su lado (nunca sin límite ni cerrado)
    assert [first.hit("k", 2, 60) for _ in range(3)] == [None, None, 60]
    assert [second.hit("k", 2, 60) for _ in range(3)] == [None, None, 60]
    server.down = False
    first.reset()
    assert first.hit("k", 1, 60) is None and first.hit("k", 1, 60) == 60 and first.fallback.hit("k", 2, 60) is None


def test_the_limiter_key_hides_the_subject_and_keeps_the_rule(monkeypatch):
    key = subject_key("login:email:ana@empresa.com")
    assert key.startswith("login:") and "ana" not in key and len(key) == len("login:") + 32
    assert key == subject_key("login:email:ana@empresa.com") != subject_key("login:email:eva@empresa.com")
    assert subject_key("api-key:abc").startswith("api-key:") and subject_key("solo") == subject_key("solo")
    monkeypatch.setattr(settings, "RATE_LIMIT_HASH_SALT", "otra-sal")
    assert subject_key("login:email:ana@empresa.com") != key  # la huella depende de la sal del .env


def test_each_backend_builds_its_limiter():
    assert isinstance(rate_limit.build_limiter("memory"), InMemoryRateLimiter)
    assert isinstance(rate_limit.build_limiter("database"), DatabaseRateLimiter)
    redis_limiter = rate_limit.build_limiter("redis")
    assert isinstance(redis_limiter, RedisRateLimiter) and redis_limiter.cache is shared_cache()


def test_login_attempts_are_limited_across_replicas_without_personal_data_in_redis(
    client: TestClient, redis_cache, server, monkeypatch
):
    monkeypatch.setattr(rate_limit, "limiter", RedisRateLimiter(cache=redis_cache))
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 2)
    body = {"email": "admin@empresa.com", "password": "Incorrecta123"}
    codes = [client.post("/api/auth/login", json=body).status_code for _ in range(3)]
    assert codes == [401, 401, 429]
    assert all("admin" not in key and "empresa" not in key for key in server.store)  # regla 13: solo huellas
    assert any(":rl:login:" in key for key in server.store)
    server.down = True  # Redis caído a media sesión: el límite por proceso responde y nada falla
    assert client.post("/api/auth/login", json=body).status_code == 401
    assert client.get("/api/health/live").status_code == 200


def test_the_admin_sees_the_shared_cache_state_in_the_server_status(client, admin_headers, redis_cache):
    cache = client.get("/api/admin/errors/server", headers=admin_headers).json()["data"]["cache"]
    assert cache == {"backend": "redis", "target": "fake:6379/0", "status": "ok", "failures": 0}
    use_cache(None)
    assert client.get("/api/admin/errors/server", headers=admin_headers).json()["data"]["cache"]["backend"] == (
        "disabled"
    )


def test_a_configured_limiter_uses_the_process_cache_lazily(server):
    limiter = RedisRateLimiter()
    assert limiter.cache is shared_cache()
    with_cache = RedisRateLimiter(cache=SimpleNamespace(hit_window=lambda *_: 1, delete_prefix=lambda *_: 0))
    assert with_cache.hit("k", 5, 60) is None
