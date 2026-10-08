"""Limitadores de peticiones con una misma interfaz `hit(key, limit, window) -> retry_after`.

- `DatabaseRateLimiter` (RATE_LIMIT_BACKEND=database, por defecto): ventana fija en
  PostgreSQL con un UPSERT atómico. Compartido por TODOS los procesos e instancias de la API
  sin otra pieza que operar. Solo se usa en endpoints sensibles (login, refresh, verificación), no en cada petición.
- `RedisRateLimiter` (RATE_LIMIT_BACKEND=redis; el entorno completo de docker compose): la misma ventana fija en la
  caché compartida (`app/core/cache.py`: INCR + EXPIRE en una transacción, sin tocar la base). Compartido por todas
  las réplicas: con 5 instancias un atacante no tiene 5 veces más intentos. Si Redis no responde, el límite POR
  PROCESO (`InMemoryRateLimiter`) toma el relevo hasta que vuelva (regla 7): nunca se queda sin límite ni se cierra.
- `InMemoryRateLimiter` (RATE_LIMIT_BACKEND=memory): ventana deslizante por proceso; útil
  en desarrollo y pruebas (en producción no arranca: cada réplica contaría por su lado).

**Privacidad (regla 13)**: la llave que llega a cualquier limitador desde `enforce` es `regla:huella` (`subject_key`):
la regla queda legible para operar (`login`, `refresh`, `api-key`) y el sujeto (correo, IP, sesión, usuario) viaja
como huella BLAKE2 con la sal `RATE_LIMIT_HASH_SALT`. Ni Redis ni `auth.rate_limit_counters` guardan un dato personal
en claro.
"""

import hashlib
import logging
import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from fastapi import Request
from sqlalchemy import delete
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import SQLAlchemyError

from app.core.cache import SharedCache, shared_cache
from app.core.config import settings
from app.core.database import engine
from app.core.exceptions import RateLimitError
from app.models import RateLimitCounter

logger = logging.getLogger(__name__)


class RateLimiter(Protocol):
    def hit(self, key: str, limit: int, window_seconds: int) -> int | None: ...

    def reset(self) -> None: ...


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        """Registra un intento. Devuelve segundos de espera si se excede el límite."""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= now - window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, math.ceil(bucket[0] + window_seconds - now))
            bucket.append(now)
            self._maybe_cleanup(now, window_seconds)
        return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def _maybe_cleanup(self, now: float, window_seconds: int) -> None:
        if now - self._last_cleanup < 300:
            return
        self._last_cleanup = now
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= now - window_seconds]:
            del self._hits[key]


def _fixed_window(now: float, window_seconds: int) -> tuple[int, float]:
    """La ventana fija en que cae `now`: su número y cuándo termina (igual en la base y en Redis)."""
    bucket = int(now // window_seconds)
    return bucket, (bucket + 1) * window_seconds


class DatabaseRateLimiter:
    """Ventana fija: una fila por (clave, ventana) con contador incrementado atómicamente."""

    def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        now = time.time()
        bucket, window_end = _fixed_window(now, window_seconds)
        dialect = postgresql if engine.dialect.name == "postgresql" else sqlite
        stmt = dialect.insert(RateLimitCounter).values(
            key=f"{key}:{bucket}"[:255], count=1, expires_at=datetime.fromtimestamp(window_end, UTC)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[RateLimitCounter.key], set_={"count": RateLimitCounter.count + 1}
        ).returning(RateLimitCounter.count)
        try:
            # Solo el conteo (transacción mínima); los contadores viejos los depura el mantenimiento.
            with engine.begin() as conn:
                count: int = conn.execute(stmt).scalar_one()
        except SQLAlchemyError as exc:
            # Si la BD no responde, el límite no debe tumbar el login (falla abierto y se registra).
            logger.warning("Rate limit no disponible (%s); se permite la petición", exc.__class__.__name__)
            return None
        return max(1, math.ceil(window_end - now)) if count > limit else None

    def reset(self) -> None:
        with engine.begin() as conn:
            conn.execute(delete(RateLimitCounter))


class RedisRateLimiter:
    """Ventana fija en la caché compartida: `rl:<llave>:<ventana>` con INCR + EXPIRE atómicos. Las llaves viven el
    doble de la ventana (una diferencia de reloj entre réplicas de hasta una ventana no pierde el contador) y vencen
    solas: nada que depurar. Sin Redis, el límite por proceso responde (y lo registra la capa de la caché una vez)."""

    def __init__(self, cache: SharedCache | None = None, fallback: RateLimiter | None = None) -> None:
        self._cache = cache
        self.fallback: RateLimiter = fallback or InMemoryRateLimiter()

    @property
    def cache(self) -> SharedCache:
        return self._cache or shared_cache()

    def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        now = time.time()
        bucket, window_end = _fixed_window(now, window_seconds)
        count = self.cache.hit_window(f"rl:{key}:{bucket}", window_seconds * 2)
        if count is None:
            return self.fallback.hit(key, limit, window_seconds)
        return max(1, math.ceil(window_end - now)) if count > limit else None

    def reset(self) -> None:
        self.cache.delete_prefix("rl:*")
        self.fallback.reset()


def build_limiter(backend: str) -> RateLimiter:
    """El limitador de `RATE_LIMIT_BACKEND` (la configuración ya validó que `redis` tenga Redis)."""
    if backend == "database":
        return DatabaseRateLimiter()
    if backend == "redis":
        return RedisRateLimiter()
    return InMemoryRateLimiter()


limiter: RateLimiter = build_limiter(settings.RATE_LIMIT_BACKEND)


def subject_key(key: str) -> str:
    """`login:email:ana@empresa.com` → `login:<huella>`: la regla legible y el sujeto como huella BLAKE2 de 128 bits
    con la sal `RATE_LIMIT_HASH_SALT` (vacía solo en desarrollo). Dos sujetos distintos nunca comparten contador."""
    rule = key.split(":", 1)[0]
    salt = settings.RATE_LIMIT_HASH_SALT.encode()[:64]
    return f"{rule}:{hashlib.blake2b(key.encode(), key=salt, digest_size=16).hexdigest()}"


def client_ip(request: Request) -> str:
    # Con uvicorn --proxy-headers, request.client ya refleja X-Forwarded-For del proxy confiable.
    return request.client.host if request.client else "unknown"


def enforce(key: str, limit: int, window_seconds: int = 60) -> None:
    retry_after = limiter.hit(subject_key(key), limit, window_seconds)
    if retry_after is not None:
        raise RateLimitError(retry_after)


def ip_rate_limit(name: str, limit: Callable[[], int], window_seconds: int = 60) -> Callable[[Request], None]:
    """Dependencia FastAPI que limita por IP de origen."""

    def dependency(request: Request) -> None:
        enforce(f"{name}:ip:{client_ip(request)}", limit(), window_seconds)

    return dependency
