"""Redis falso (en memoria) para probar la caché compartida sin red ni servidor.

Implementa SOLO lo que usa `app/core/cache.py` (`RedisLike`): GET, SET con vigencia, DEL, SCAN con patrón y la
transacción INCR + EXPIRE NX. El reloj se controla desde la prueba (`now`) para ver vencer las llaves sin esperar, y
`down` hace que cada comando falle como si Redis no respondiera (`redis.exceptions.ConnectionError`): así se prueba el
cortacircuitos. `shared(prefix)` arma una `SharedCache` con él; dos cachés sobre el MISMO `FakeRedis` son dos réplicas
que comparten Redis.
"""

import fnmatch
import math
from collections.abc import Iterator
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.cache import SharedCache
from app.core.config import settings


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.expires: dict[str, float] = {}
        #: Reloj propio (segundos): la prueba lo adelanta para vencer llaves.
        self.now = 1_000_000.0
        #: Con True todo comando falla como si Redis no respondiera.
        self.down = False
        self.calls: list[str] = []
        self.closed = False
        self.fail_on_close = False

    # ---------------------------------------------------------------- utilería

    def _check(self, command: str) -> None:
        self.calls.append(command)
        if self.down:
            raise RedisConnectionError("Redis no responde (simulado)")

    def _alive(self, name: str) -> bool:
        if name in self.expires and self.expires[name] <= self.now:
            self.store.pop(name, None)
            self.expires.pop(name, None)
        return name in self.store

    def ttl(self, name: str) -> int:
        """Segundos que le quedan a la llave (-1 sin vencimiento, -2 si no existe), como Redis."""
        if not self._alive(name):
            return -2
        return math.ceil(self.expires[name] - self.now) if name in self.expires else -1

    # ---------------------------------------------------------------- comandos

    def get(self, name: str) -> bytes | None:
        self._check("get")
        return self.store[name] if self._alive(name) else None

    def set(self, name: str, value: bytes, ex: int | None = None) -> bool:
        self._check("set")
        self.store[name] = bytes(value)
        if ex is None:
            self.expires.pop(name, None)
        else:
            self.expires[name] = self.now + ex
        return True

    def delete(self, *names: str) -> int:
        self._check("delete")
        removed = 0
        for name in names:
            if self._alive(name):
                del self.store[name]
                self.expires.pop(name, None)
                removed += 1
        return removed

    def scan_iter(self, match: str | None = None, count: int | None = None) -> Iterator[bytes]:
        self._check("scan")
        for name in list(self.store):
            if self._alive(name) and (match is None or fnmatch.fnmatchcase(name, match)):
                yield name.encode()

    def incr(self, name: str) -> int:
        self._check("incr")
        value = int(self.store[name]) + 1 if self._alive(name) else 1
        self.store[name] = str(value).encode()
        return value

    def expire(self, name: str, time: int, nx: bool = False) -> bool:
        self._check("expire")
        if not self._alive(name) or (nx and name in self.expires):
            return False
        self.expires[name] = self.now + time
        return True

    def pipeline(self, transaction: bool = True) -> _Pipeline:
        return _Pipeline(self)

    def close(self) -> None:
        if self.fail_on_close:
            raise RedisConnectionError("cerrar falló (simulado)")
        self.closed = True


class _Pipeline:
    """MULTI/EXEC: acumula comandos y los ejecuta juntos (en memoria no hay concurrencia que serializar)."""

    def __init__(self, server: FakeRedis) -> None:
        self._server = server
        self._queued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def incr(self, name: str) -> _Pipeline:
        self._queued.append(("incr", (name,), {}))
        return self

    def expire(self, name: str, time: int, nx: bool = False) -> _Pipeline:
        self._queued.append(("expire", (name, time), {"nx": nx}))
        return self

    def execute(self) -> list[Any]:
        return [getattr(self._server, command)(*args, **kwargs) for command, args, kwargs in self._queued]


def shared(server: FakeRedis, *, prefix: str = "test", retry_seconds: float | None = None) -> SharedCache:
    """Una caché compartida (una "réplica") sobre ese Redis falso."""
    return SharedCache(
        server,
        prefix=prefix,
        retry_seconds=settings.REDIS_RETRY_SECONDS if retry_seconds is None else retry_seconds,
        target="fake:6379/0",
    )
