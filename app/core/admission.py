"""Control de admisión adaptativo: la capacidad sigue a la demanda.

Antes cada proceso admitía un número FIJO de peticiones a la vez y las demás esperaban en orden de
llegada: con un pico, todo se volvía lento por igual y lo crítico (identificar, iniciar sesión)
esperaba detrás de un tablero o un listado. Ahora, en cada proceso (cada instancia se adapta a SU
carga, sin estado compartido):

1. **Demanda por grupo de API** (método + ruta sin ids, p. ej. `POST checkpoint/identify/face`):
   tasa reciente con decaimiento exponencial (vida media ADMISSION_DEMAND_HALF_LIFE_SECONDS) y
   latencia típica y actual de cada grupo. Solo cuentan las respuestas exitosas (un 401 o un 503
   inmediatos no dicen nada de la capacidad) y lo "típico" se aprende solo con holgura: una
   sobrecarga sostenida nunca se vuelve lo normal.
2. **Límite adaptativo** entre MIN_CONCURRENT_REQUESTS y MAX_CONCURRENT_REQUESTS: si, con la
   concurrencia llena, las APIs tardan bastante más que su normal (fila en la BD, el CPU o los
   workers faciales), el límite baja (×0.9); si están sanas y hay demanda esperando, sube (+10 %).
   Así trabaja cerca de la capacidad real de la máquina sin saturarla.
3. **Fila con prioridad**: primero lo crítico (identidad y acceso), luego lo normal y al final lo de
   fondo (tableros, documentación). Dentro de cada nivel pasan primero las APIs con MÁS demanda,
   pero ningún grupo ocupa más de la mitad de la fila (una avalancha de inicios de sesión no deja
   sin lugar a todo lo demás).
4. **Descarte controlado**: con la fila llena, una petición de más prioridad desplaza a la de menos
   (que recibe 503 SERVER_BUSY con Retry-After, reintentable); lo que espera demasiado, también.
   Cada nivel espera a lo más su parte del tiempo de la fila (crítico todo, normal la mitad, de fondo
   un cuarto) y la que no alcanzaría se rechaza al llegar, según las que pasarán antes y el ritmo real
   de salida.
5. **Memoria acotada**: a lo más MAX_GROUPS grupos; los inactivos se olvidan y, si aun así no caben,
   las rutas nuevas comparten un grupo por nivel (una ruta crítica sigue siendo crítica).

Todo corre en el event loop del proceso (un solo hilo): sin candados.
"""

import asyncio
import heapq
import itertools
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum


class Tier(IntEnum):
    """Nivel de prioridad de un grupo de APIs."""

    BACKGROUND = 0
    NORMAL = 1
    CRITICAL = 2


#: Identidad y acceso: lo que un empleado o un validador espera de pie frente a la cámara o al
#: iniciar sesión. Se atiende primero y se descarta al último.
CRITICAL_PREFIXES = (
    "POST auth/login",
    "POST auth/refresh",
    "POST auth/company",
    "POST checkpoint/",
    # El kiosco de un sitio: sin su código vigente, la entrada y la salida en ese sitio no se confirman (antifraude 2b).
    "POST kiosk/code",
    "POST verification/",
    "POST face/challenge",
    "POST enrollment/face",
    "POST users/me/qr",
    "POST me/attendance/",
    "GET users/me/qr",
    "GET users/me",
)
#: Rutas FACIALES (regla 18 de la raíz; excepción que decidió el dueño del producto el 2026-10-06): las que esperan un
#: worker del motor facial y analizan varias capturas (registro facial del empleado y en persona, verificación,
#: identificación del validador, registro de asistencia y la revisión de una captura) y el reto que las empieza. Su
#: petición lenta se cuenta desde `SLOW_REQUEST_FACE_THRESHOLD_MS` (2 500 ms) y la de las demás desde
#: `SLOW_REQUEST_THRESHOLD_MS` (1 000 ms). Una ruta nueva que use el motor (`Pipeline`) se agrega aquí:
#: `tests/test_slow_requests.py` falla si esta lista y las rutas que usan el motor no coinciden.
FACE_PREFIXES = (
    "POST face/",
    "POST enrollment/face",
    "POST employees/{id}/face/enroll",
    "POST employees/{id}/face/verify",
    "POST verification/face",
    "POST checkpoint/identify/face",
    "POST me/attendance/",
)
#: Lo que puede esperar: tableros, resúmenes, documentación y los reportes de fallas del navegador
#: (si el servidor está saturado, se descartan primero: la app no los reintenta). El tablero de
#: asistencia se refresca solo cada pocos segundos y la seguridad facial, el consumo y el resumen de la
#: cobranza del ADMIN son estadísticas: al saturarse ceden su lugar a checar, identificar e iniciar sesión.
#: Las fotos de perfil son adorno: al saturarse la app muestra las iniciales. La pantalla "Rendimiento" del ADMIN
#: (sus lecturas) y los lotes de rendimiento del navegador también son estadísticas. Descargar un documento de la
#: empresa (hasta COMPANY_DOCUMENT_MAX_MB descifrados y en base64) puede esperar a que pase la saturación; el del
#: ADMIN comparte el grupo de `GET admin/companies/{id}` (los grupos tienen 3 segmentos) y su tráfico es mínimo.
BACKGROUND_PREFIXES = (
    "GET users/{id}/avatar",
    "GET documents/{id}/file",
    "GET admin/stats",
    "GET admin/face-security",
    "GET admin/fraud-cases",
    "GET admin/usage",
    "GET admin/billing/overview",
    "GET admin/performance",
    "GET attendance/board",
    "POST client-errors",
    "POST telemetry/",
    "GET docs",
    "GET redoc",
    "GET openapi.json",
)

_ID = re.compile(r"^\d+$|^[0-9a-f]{32}$|^[0-9a-f]{8}-[0-9a-f-]{27}$")
#: Grupos que se siguen por separado (rutas reales, no cada URL que alguien invente).
MAX_GROUPS = 256
OTHER = "OTHER"
#: Fracción máxima de la fila que puede ocupar un solo grupo (equidad entre APIs).
GROUP_QUEUE_SHARE = 0.5
#: Fracción de REQUEST_QUEUE_TIMEOUT_SECONDS que espera cada nivel en la fila: lo crítico espera
#: todo; un listado, la mitad; un tablero, un cuarto (mejor un 503 rápido que reintentar a tiempo).
TIER_WAIT = {Tier.CRITICAL: 1.0, Tier.NORMAL: 0.5, Tier.BACKGROUND: 0.25}
#: Demanda (decaída) debajo de la cual un grupo está inactivo y se puede olvidar.
_IDLE_DEMAND = 0.5


def overflow_group(tier: Tier) -> str:
    """Grupo compartido de las rutas que ya no caben, uno por nivel."""
    return f"{OTHER} {tier.name}"


def group_of(method: str, path: str, prefix: str = "/api", depth: int | None = 3) -> str:
    """`GET /api/employees/12/qr` → `GET employees/{id}/qr` (a lo más `depth` segmentos; None = todos)."""
    if path.startswith(prefix):
        path = path[len(prefix) :]
    parts = ["{id}" if _ID.match(part) else part for part in path.strip("/").split("/") if part][:depth]
    return f"{method} {'/'.join(parts)}"


def is_face_route(method: str, path: str) -> bool:
    """¿Es una ruta facial (`FACE_PREFIXES`)? Con la ruta COMPLETA (ids → `{id}`): `employees/{id}/face/verify` lo es y
    `employees/{id}/face/reset` no (pide otro registro facial, no analiza capturas)."""
    return group_of(method, path, depth=None).startswith(FACE_PREFIXES)


def tier_of(group: str) -> Tier:
    if group.startswith(CRITICAL_PREFIXES):
        return Tier.CRITICAL
    if group.startswith(BACKGROUND_PREFIXES):
        return Tier.BACKGROUND
    return Tier.NORMAL


@dataclass
class GroupStats:
    """Demanda y latencia de un grupo de APIs."""

    tier: Tier
    #: Llegadas con decaimiento exponencial (≈ peticiones en la última vida media).
    demand: float = 0.0
    last_arrival: float = 0.0
    #: Latencia típica (aprendida con holgura) y actual (media rápida), en segundos.
    typical: float | None = None
    current: float | None = None
    shed: int = 0
    #: Peticiones de este grupo esperando en la fila.
    waiting: int = 0

    def decayed(self, now: float, half_life: float) -> float:
        return self.demand * math.pow(0.5, max(0.0, now - self.last_arrival) / half_life)


@dataclass(order=True)
class _Waiter:
    #: Orden del heap: más prioridad primero (por eso negativos), luego llegada.
    key: tuple[int, float, int]
    stats: GroupStats = field(compare=False)
    future: asyncio.Future[bool] = field(compare=False)


@dataclass(frozen=True)
class AdmissionSettings:
    min_limit: int
    max_limit: int
    queue_timeout: float
    #: 0 = automático (el doble del límite vigente).
    max_waiting: int = 0
    half_life: float = 60.0
    #: Cada cuánto se reconsidera el límite (s).
    adjust_every: float = 1.0
    #: Latencia actual / típica a partir de la cual hay congestión (y debajo de la cual hay holgura).
    #: Tolerante a propósito: con la máquina saturada desde fuera (otro proceso, la red, una
    #: avalancha de rechazos) todo se vuelve algo más lento aunque la concurrencia no sea la causa.
    congested_ratio: float = 2.5
    healthy_ratio: float = 1.5
    #: Peso de cada respuesta (con holgura) en la latencia típica.
    baseline_weight: float = 0.02


class AdmissionController:
    """Límite adaptativo + fila con prioridad por nivel y demanda (un proceso, un event loop)."""

    def __init__(self, settings: AdmissionSettings, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.clock = clock
        self.limit = settings.max_limit
        self.in_flight = 0
        self.admitted = 0
        self.shed = 0
        self.groups: dict[str, GroupStats] = {}
        self._waiting: list[_Waiter] = []
        self._pending = 0
        self._sequence = itertools.count()
        self._ratio: float | None = None
        self._last_adjust = clock()
        self._last_evict = -math.inf
        #: Cuánto tarda una respuesta exitosa (todas las APIs): con el límite, da el ritmo de la fila.
        self._service: float | None = None
        #: Peticiones esperando por nivel (para estimar cuántas pasarán antes que una nueva).
        self._waiting_by_tier = dict.fromkeys(Tier, 0)
        #: Suma de la demanda de todos los grupos (decae al mismo ritmo que cada uno: O(1) por llegada).
        self._total_demand = 0.0
        self._total_at = clock()

    # ---------- Admisión ----------

    async def acquire(self, group: str) -> bool:
        """True = puede pasar (debe llamar a `release`); False = se descarta (503)."""
        stats = self._arrive(group)
        if self.in_flight < self.limit and self._pending == 0:
            self.in_flight += 1
            self.admitted += 1
            return True
        if self._hopeless(stats):
            return self._reject(stats)
        waiter = self._enqueue(stats)
        if waiter is None:
            return self._reject(stats)
        try:
            granted = await asyncio.wait_for(asyncio.shield(waiter.future), timeout=self._budget(stats.tier))
        except TimeoutError:
            if waiter.future.done():  # justo al vencer se le dio lugar (o se le desplazó)
                return waiter.future.result() or self._reject(stats)
            self._leave(waiter)
            return self._reject(stats)
        except asyncio.CancelledError:  # el cliente se fue mientras esperaba
            if not waiter.future.done():
                self._leave(waiter)
            elif waiter.future.result():
                self.release(group, None)
            raise
        return granted or self._reject(stats)

    def release(self, group: str, elapsed: float | None) -> None:
        """Libera el lugar y registra cuánto tardó la petición (sin contar su espera en la fila).
        `elapsed` = None si no respondió con éxito (no dice nada de la capacidad)."""
        self.in_flight -= 1
        if elapsed is not None:
            self._observe(group, elapsed)
        self._grant()

    # ---------- Estado (sondas de salud) ----------

    def snapshot(self, top: int = 5) -> dict[str, object]:
        now = self.clock()
        half_life = self.settings.half_life
        busiest = sorted(self.groups.items(), key=lambda item: item[1].decayed(now, half_life), reverse=True)[:top]
        return {
            "limit": self.limit,
            "bounds": [self.settings.min_limit, self.settings.max_limit],
            "in_flight": self.in_flight,
            "waiting": self._pending,
            "admitted": self.admitted,
            "shed": self.shed,
            "latency_ratio": None if self._ratio is None else round(self._ratio, 3),
            "top_demand": [
                {
                    "api": name,
                    "tier": stats.tier.name,
                    "recent_requests": round(stats.decayed(now, half_life), 1),
                    "latency_ms": None if stats.current is None else round(stats.current * 1000, 1),
                    "shed": stats.shed,
                }
                for name, stats in busiest
            ],
        }

    # ---------- Internos ----------

    def _arrive(self, group: str) -> GroupStats:
        stats = self.groups.get(group) or self._register(group)
        now = self.clock()
        stats.demand = stats.decayed(now, self.settings.half_life) + 1.0
        stats.last_arrival = now
        self._total_demand = self._total(now) + 1.0
        self._total_at = now
        return stats

    def _stats(self, group: str) -> GroupStats | None:
        """Estadística del grupo (o del grupo compartido de su nivel si ya no cupo)."""
        return self.groups.get(group) or self.groups.get(overflow_group(tier_of(group)))

    def _register(self, group: str) -> GroupStats:
        tier = tier_of(group)
        if len(self.groups) >= MAX_GROUPS:
            self._evict_idle()
        if len(self.groups) >= MAX_GROUPS:
            group = overflow_group(tier)
        return self.groups.setdefault(group, GroupStats(tier))

    def _evict_idle(self) -> None:
        """Olvida los grupos inactivos (a lo más una vez por segundo: con todos activos, no se recorre
        la tabla en cada petición)."""
        now = self.clock()
        if now - self._last_evict < 1.0:
            return
        self._last_evict = now
        half_life = self.settings.half_life
        for name, stats in list(self.groups.items()):
            if stats.waiting == 0 and stats.decayed(now, half_life) < _IDLE_DEMAND:
                del self.groups[name]

    def _total(self, now: float) -> float:
        return self._total_demand * math.pow(0.5, max(0.0, now - self._total_at) / self.settings.half_life)

    def _share(self, stats: GroupStats) -> float:
        """Fracción de la demanda reciente del proceso que es de este grupo (0 a 1)."""
        now = self.clock()
        total = self._total(now)
        return min(1.0, stats.decayed(now, self.settings.half_life) / total) if total else 0.0

    def _budget(self, tier: Tier) -> float:
        """Cuánto puede esperar en la fila una petición de este nivel."""
        return self.settings.queue_timeout * TIER_WAIT[tier]

    def _hopeless(self, stats: GroupStats) -> bool:
        """¿Esperaría más que lo que su nivel puede esperar? Entonces se rechaza YA (503 inmediato, el
        cliente reintenta) en vez de ocupar la fila para fallar después. La espera se estima con las que
        pasarán antes (las de su nivel o más) y el ritmo real de salida (límite / tiempo de respuesta)."""
        if self._service is None or self._pending == 0:
            return False  # sin fila formada siempre se espera: el rechazo inmediato es solo para picos
        ahead = sum(count for tier, count in self._waiting_by_tier.items() if tier >= stats.tier)
        expected_wait = (ahead + 1) / max(1, self.limit) * self._service
        return expected_wait > self._budget(stats.tier)

    def _capacity(self) -> int:
        return self.settings.max_waiting or 2 * self.limit

    def _enqueue(self, stats: GroupStats) -> _Waiter | None:
        """Entra a la fila; si está llena, desplaza a quien tenga menos prioridad (o no entra). Un grupo
        que ya ocupa su parte de la fila no entra (ni desplaza a nadie)."""
        capacity = self._capacity()
        if stats.waiting >= max(1, int(capacity * GROUP_QUEUE_SHARE)):
            return None
        key = (-int(stats.tier), -self._share(stats), next(self._sequence))
        if self._pending >= capacity:
            weakest = self._weakest()
            if weakest is None or weakest.key <= key:
                return None
            weakest.future.set_result(False)  # cede su lugar: recibe 503 reintentable
            self._dequeued(weakest)
        waiter = _Waiter(key, stats, asyncio.get_running_loop().create_future())
        heapq.heappush(self._waiting, waiter)
        self._pending += 1
        stats.waiting += 1
        self._waiting_by_tier[stats.tier] += 1
        return waiter

    def _weakest(self) -> _Waiter | None:
        self._compact()
        return max(self._waiting, default=None)

    def _compact(self) -> None:
        """Quita de la fila las entradas que ya no esperan (vencidas, canceladas o desplazadas)."""
        self._waiting = [w for w in self._waiting if not w.future.done()]
        heapq.heapify(self._waiting)

    def _dequeued(self, waiter: _Waiter) -> None:
        self._pending -= 1
        waiter.stats.waiting -= 1
        self._waiting_by_tier[waiter.stats.tier] -= 1

    def _grant(self) -> None:
        while self.in_flight < self.limit and self._waiting:
            waiter = heapq.heappop(self._waiting)
            if waiter.future.done():
                continue
            self._dequeued(waiter)
            self.in_flight += 1
            self.admitted += 1
            waiter.future.set_result(True)

    def _leave(self, waiter: _Waiter) -> None:
        """Sale de la fila sin lugar (venció o el cliente se fue). Su entrada se omite al repartir y,
        si se acumulan muchas así, se compacta la fila (no crece con entradas muertas)."""
        waiter.future.cancel()
        self._dequeued(waiter)
        if len(self._waiting) > 2 * self._pending + 64:
            self._compact()

    def _reject(self, stats: GroupStats) -> bool:
        self.shed += 1
        stats.shed += 1
        return False

    def retry_after(self) -> int:
        """Segundos sugeridos para reintentar (más fila = más espera), de 1 a 10."""
        return min(10, 1 + self._pending // max(1, self.limit))

    def _saturated(self) -> bool:
        return self._pending > 0 or self.in_flight >= 0.8 * self.limit

    def _observe(self, group: str, elapsed: float) -> None:
        stats = self._stats(group)
        if stats is None:  # grupo olvidado mientras atendía una petición larga
            return
        if stats.typical is None or stats.current is None:
            stats.typical = stats.current = elapsed
        else:
            stats.current = 0.8 * stats.current + 0.2 * elapsed
            if not self._saturated():
                # Lo "normal" se aprende solo con holgura (sube o baja igual): una API que de verdad
                # se volvió más lenta cambia su normal, pero una sobrecarga nunca se vuelve normal.
                weight = self.settings.baseline_weight
                stats.typical = (1 - weight) * stats.typical + weight * elapsed
        self._service = elapsed if self._service is None else 0.9 * self._service + 0.1 * elapsed
        ratio = stats.current / max(stats.typical, 1e-4)
        self._ratio = ratio if self._ratio is None else 0.9 * self._ratio + 0.1 * ratio
        self._adjust()

    def _adjust(self) -> None:
        now = self.clock()
        if self._ratio is None or now - self._last_adjust < self.settings.adjust_every:
            return
        self._last_adjust = now
        s = self.settings
        saturated = self._saturated()
        if self._ratio >= s.congested_ratio and saturated:
            # Solo si NUESTRA concurrencia está llena: si sobran lugares, la lentitud viene de fuera y
            # recortar solo bajaría lo que se atiende (el límite se desplomaría sin ayudar).
            self.limit = max(s.min_limit, math.floor(self.limit * 0.9))
        elif self._ratio <= s.healthy_ratio and saturated:
            self.limit = min(s.max_limit, self.limit + max(1, math.ceil(self.limit * 0.1)))
        self._grant()


def build_controller() -> AdmissionController:
    """El controlador del proceso con la configuración vigente (uno por proceso de la API)."""
    from app.core.config import settings

    return AdmissionController(
        AdmissionSettings(
            min_limit=min(settings.MIN_CONCURRENT_REQUESTS, settings.MAX_CONCURRENT_REQUESTS),
            max_limit=settings.MAX_CONCURRENT_REQUESTS,
            queue_timeout=settings.REQUEST_QUEUE_TIMEOUT_SECONDS,
            max_waiting=settings.REQUEST_QUEUE_MAX,
            half_life=settings.ADMISSION_DEMAND_HALF_LIFE_SECONDS,
        )
    )


#: Controlador de este proceso (lo usan el middleware y la sonda de salud).
admission = build_controller()
