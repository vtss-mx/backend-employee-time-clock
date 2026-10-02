"""Limitadores de peticiones con una misma interfaz `hit(key, limit, window) -> retry_after`.

- `DatabaseRateLimiter` (RATE_LIMIT_BACKEND=database, por defecto): ventana fija en
  PostgreSQL con un UPSERT atómico. Compartido por TODOS los procesos e instancias de la API
  (escala horizontalmente sin Redis). Solo se usa en endpoints sensibles (login, refresh,
  verificación), no en cada petición.
- `InMemoryRateLimiter` (RATE_LIMIT_BACKEND=memory): ventana deslizante por proceso; útil
  en desarrollo y pruebas.
"""

import logging
import math
import random
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fastapi import Request
from sqlalchemy import delete
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import SQLAlchemyError

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


class DatabaseRateLimiter:
    """Ventana fija: una fila por (clave, ventana) con contador incrementado atómicamente."""

    def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        now = time.time()
        bucket = int(now // window_seconds)
        window_end = (bucket + 1) * window_seconds
        dialect = postgresql if engine.dialect.name == "postgresql" else sqlite
        stmt = dialect.insert(RateLimitCounter).values(
            key=f"{key}:{bucket}"[:255], count=1, expires_at=datetime.fromtimestamp(window_end, UTC)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[RateLimitCounter.key], set_={"count": RateLimitCounter.count + 1}
        ).returning(RateLimitCounter.count)
        try:
            with engine.begin() as conn:
                count: int = conn.execute(stmt).scalar_one()
                if random.random() < 0.01:  # noqa: S311 - muestreo de limpieza, no criptográfico
                    conn.execute(
                        delete(RateLimitCounter).where(
                            RateLimitCounter.expires_at < datetime.now(UTC) - timedelta(minutes=5)
                        )
                    )
        except SQLAlchemyError as exc:
            # Si la BD no responde, el límite no debe tumbar el login (falla abierto y se registra).
            logger.warning("Rate limit no disponible (%s); se permite la petición", exc.__class__.__name__)
            return None
        return max(1, math.ceil(window_end - now)) if count > limit else None

    def reset(self) -> None:
        with engine.begin() as conn:
            conn.execute(delete(RateLimitCounter))


limiter: RateLimiter = DatabaseRateLimiter() if settings.RATE_LIMIT_BACKEND == "database" else InMemoryRateLimiter()


def client_ip(request: Request) -> str:
    # Con uvicorn --proxy-headers, request.client ya refleja X-Forwarded-For del proxy confiable.
    return request.client.host if request.client else "unknown"


def enforce(key: str, limit: int, window_seconds: int = 60) -> None:
    retry_after = limiter.hit(key, limit, window_seconds)
    if retry_after is not None:
        raise RateLimitError(retry_after)


def ip_rate_limit(name: str, limit: Callable[[], int], window_seconds: int = 60) -> Callable[[Request], None]:
    """Dependencia FastAPI que limita por IP de origen."""

    def dependency(request: Request) -> None:
        enforce(f"{name}:ip:{client_ip(request)}", limit(), window_seconds)

    return dependency
