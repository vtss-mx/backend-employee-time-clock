"""Caché compartida entre réplicas: Redis con cortacircuitos (decisión del dueño del producto, 2026-10-07).

Única capa de la API que habla con Redis. Qué guarda y qué no (regla 13 de la raíz, privacidad por diseño):

- la instantánea de los catálogos (`catalog_service`: datos de la plataforma, sin nada personal), versionada por el
  código que la escribió, con vigencia `CATALOG_REDIS_SECONDS`;
- los contadores de los límites de peticiones (`rate_limit.RedisRateLimiter`): la llave es la HUELLA del sujeto (con
  `RATE_LIMIT_HASH_SALT`), nunca un correo ni una IP en claro.

Nada más: ni sesiones, ni retos, ni datos de empresa. Redis no es la fuente de verdad de nada (es PostgreSQL): lo que
hay aquí siempre se puede volver a calcular.

Cómo falla (regla 7, tolerancia a fallas): toda llamada tiene tiempo límite corto (`REDIS_CONNECT_TIMEOUT_MS`,
`REDIS_TIMEOUT_MS`) y el cliente no reintenta por su cuenta. Si Redis no responde, el **cortacircuitos** se abre:
durante `REDIS_RETRY_SECONDS` cada operación responde "sin dato" (None) en microsegundos, sin tocar la red, y después
se vuelve a intentar UNA vez. La falla se registra UNA sola vez por caída como error del sistema (regla 4:
`logger.error` con un texto estable, nunca por petición) y su recuperación como información. Quien usa la caché sigue
con su respaldo (la caché local del proceso, la base de datos, el límite por proceso): Redis caído nunca tumba nada ni
agrega latencia.

Apagado: con `REDIS_HOST` y `REDIS_URL` vacíos (la topología mínima del dueño: solo db + API) la caché está
`enabled=False` y todo responde None sin hacer nada. La URL (lleva la contraseña) no se escribe en ningún log.
"""

import json
import logging
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from typing import Any, Protocol

import redis
from redis import BlockingConnectionPool
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from app.core.config import settings
from app.core.observability import observed

logger = logging.getLogger(__name__)

#: Primer byte de un flujo zlib (cabecera 0x78): un JSON nunca empieza así, por eso basta para distinguirlos.
_ZLIB_HEADER = b"\x78"


class RedisLike(Protocol):
    """Lo único que esta capa usa del cliente de Redis (`tests/redis_support.FakeRedis` lo implementa en memoria)."""

    def get(self, name: str) -> Any: ...

    def set(self, name: str, value: bytes, ex: int | None = None) -> Any: ...

    def delete(self, *names: str) -> Any: ...

    def scan_iter(self, match: str | None = None, count: int | None = None) -> Iterator[Any]: ...

    def pipeline(self, transaction: bool = True) -> Any: ...

    def close(self) -> None: ...


def encode(value: Any, level: int) -> bytes:
    """Un valor JSON como bytes; con `level` > 0, comprimido con zlib (una instantánea de catálogos de ~1 MB queda en
    una décima parte: menos red y menos memoria de Redis, por unos milisegundos de CPU una vez por recarga)."""
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    return zlib.compress(raw, level) if level > 0 else raw


def decode(raw: bytes) -> Any:
    """Lo inverso de `encode`; `ValueError` o `zlib.error` si el contenido no es lo que se escribió."""
    data = zlib.decompress(raw) if raw[:1] == _ZLIB_HEADER else raw
    return json.loads(data)


class SharedCache:
    """Redis detrás de un cortacircuitos. Todo método responde None (o False) si Redis está apagado o no responde."""

    def __init__(self, client: RedisLike | None, *, prefix: str, retry_seconds: float, target: str = "") -> None:
        self._client = client
        self._prefix = prefix
        self._retry_seconds = retry_seconds
        self._target = target
        self._lock = threading.Lock()
        #: Hasta cuándo (reloj monótono) no se habla con Redis tras una falla.
        self._open_until = 0.0
        #: Hay una falla sin recuperar: la siguiente respuesta buena se anota como recuperación.
        self._failing = False
        #: Fallas acumuladas desde el arranque (estado del servidor del ADMIN).
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def available(self) -> bool:
        """Encendida y sin el circuito abierto en este instante."""
        with self._lock:
            return self._client is not None and time.monotonic() >= self._open_until

    def describe(self) -> dict[str, Any]:
        """Para el ADMIN (Estado del servidor): dónde está y cómo va; nunca la contraseña."""
        if self._client is None:
            return {"backend": "disabled", "target": None, "status": "disabled", "failures": 0}
        with self._lock:
            status = "ok" if not self._failing and time.monotonic() >= self._open_until else "unavailable"
        return {"backend": "redis", "target": self._target, "status": status, "failures": self.failures}

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    def _call[T](self, operation: str, command: Callable[[RedisLike], T]) -> T | None:
        """Ejecuta `command` con el cliente si el circuito está cerrado; una falla lo abre y responde None."""
        client = self._client
        if client is None:
            return None
        with self._lock:
            if time.monotonic() < self._open_until:
                return None
        try:
            with observed(f"cache.{operation}"):
                result = command(client)
        except (RedisError, OSError) as exc:
            self._trip(operation, exc)
            return None
        self._recover()
        return result

    def _trip(self, operation: str, exc: Exception) -> None:
        with self._lock:
            self._open_until = time.monotonic() + self._retry_seconds
            self.failures += 1
            first = not self._failing
            self._failing = True
        if first:
            logger.error(
                "Redis (%s) no responde en %s: %s. La caché compartida se apaga %g s y se reintenta; la aplicación "
                "sigue con su caché local",
                self._target,
                operation,
                exc.__class__.__name__,
                self._retry_seconds,
            )
        else:
            logger.debug("Redis (%s) sigue sin responder en %s: %s", self._target, operation, exc.__class__.__name__)

    def _recover(self) -> None:
        with self._lock:
            recovered, self._failing = self._failing, False
        if recovered:
            logger.info("Redis (%s) volvió a responder: la caché compartida sigue", self._target)

    # ---------------------------------------------------------------- operaciones

    def get_json(self, key: str) -> Any | None:
        """El valor JSON guardado en `key`; None si no está, si Redis no responde o si el contenido es ilegible (se
        descarta y quien lo pidió lo vuelve a calcular y a escribir: la caché se cura sola)."""
        raw = self._call("get", lambda client: client.get(self._key(key)))
        if raw is None:
            return None
        try:
            return decode(raw)
        except ValueError, zlib.error, TypeError:
            logger.warning("Valor ilegible en Redis (%s): se descarta y se vuelve a calcular", key)
            self.delete(key)
            return None

    def set_json(self, key: str, value: Any, ttl_seconds: float) -> bool:
        """Guarda `value` con vigencia `ttl_seconds` (al menos 1 s). False si Redis está apagado o no respondió."""
        payload = encode(value, settings.REDIS_COMPRESSION_LEVEL)
        result = self._call("set", lambda client: client.set(self._key(key), payload, ex=max(1, int(ttl_seconds))))
        return result is not None

    def delete(self, *keys: str) -> bool:
        """Borra esas llaves (las que no existen no son una falla). False si Redis está apagado o no respondió."""
        return self._call("delete", lambda client: client.delete(*(self._key(key) for key in keys))) is not None

    def delete_prefix(self, pattern: str) -> int | None:
        """Borra las llaves que casan con `pattern` (p. ej. `catalogs:*`) y dice cuántas; recorre el espacio de llaves
        con SCAN (sin bloquear a Redis), solo en operación (al migrar), nunca en una petición."""

        def run(client: RedisLike) -> int:
            # SCAN devuelve las llaves como bytes (el cliente no decodifica respuestas): se nombran como texto.
            found = client.scan_iter(match=self._key(pattern), count=500)
            names = [name.decode() if isinstance(name, bytes) else str(name) for name in found]
            return int(client.delete(*names)) if names else 0

        return self._call("delete_prefix", run)

    def hit_window(self, key: str, expire_seconds: int) -> int | None:
        """Suma 1 a la llave y, si es nueva, le pone su vencimiento, en UNA transacción (INCR + EXPIRE NX): devuelve
        cuántas veces va. Es el contador de ventana fija de los límites de peticiones."""

        def run(client: RedisLike) -> int:
            name = self._key(key)
            pipe = client.pipeline(transaction=True)
            pipe.incr(name)
            pipe.expire(name, expire_seconds, nx=True)
            count, _ = pipe.execute()
            return int(count)

        return self._call("incr", run)

    def close(self) -> None:
        """Cierra el pool al apagar el proceso (una falla al cerrar no es una falla del servicio)."""
        if self._client is None:
            return
        try:
            self._client.close()
        except (RedisError, OSError) as exc:
            logger.debug("Al cerrar Redis: %s", exc.__class__.__name__)


def build_client(url: str) -> redis.Redis:
    """El cliente de redis-py con tiempos límite cortos y SIN reintentos propios (por omisión reintenta 10 veces con
    espera creciente: una caída de Redis frenaría cada petición). El cortacircuitos de `SharedCache` decide. El pool es
    BLOQUEANTE: con las REDIS_MAX_CONNECTIONS ocupadas, una operación espera a lo más REDIS_TIMEOUT_MS una libre en
    lugar de fallar al instante (medido en perf/scale: el pool simple abría el circuito por un pico de peticiones, no
    por una falla de Redis)."""
    pool = BlockingConnectionPool.from_url(
        url,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        timeout=settings.REDIS_TIMEOUT_MS / 1000,
        socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT_MS / 1000,
        socket_timeout=settings.REDIS_TIMEOUT_MS / 1000,
        retry=Retry(NoBackoff(), 0),
        decode_responses=False,
    )
    return redis.Redis(connection_pool=pool)


def load_cache() -> SharedCache:
    """La caché según la configuración: apagada sin REDIS_HOST/REDIS_URL (lo dice una vez en el log, sin la URL)."""
    url = settings.redis_url
    if not url:
        logger.info("Caché compartida (Redis): apagada (REDIS_HOST y REDIS_URL vacíos); cada proceso usa la suya")
        return SharedCache(None, prefix=settings.REDIS_KEY_PREFIX, retry_seconds=settings.REDIS_RETRY_SECONDS)
    target = settings.redis_target
    logger.info("Caché compartida (Redis): %s, prefijo %s", target, settings.REDIS_KEY_PREFIX)
    return SharedCache(
        build_client(url), prefix=settings.REDIS_KEY_PREFIX, retry_seconds=settings.REDIS_RETRY_SECONDS, target=target
    )


_lock = threading.Lock()
_cache: SharedCache | None = None


def shared_cache() -> SharedCache:
    """La caché compartida de este proceso (se arma una vez)."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = load_cache()
        return _cache


def use_cache(instance: SharedCache | None) -> None:
    """Fija la caché del proceso (pruebas: una con Redis falso); None la vuelve a armar con la configuración."""
    global _cache
    with _lock:
        _cache = instance
