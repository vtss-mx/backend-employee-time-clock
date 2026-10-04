"""Control de admisión adaptativo: la capacidad sigue a la demanda (app/core/admission.py)."""

import asyncio
import json
import logging
import time

import pytest

from app.core.admission import (
    MAX_GROUPS,
    AdmissionController,
    AdmissionSettings,
    Tier,
    admission,
    group_of,
    overflow_group,
    tier_of,
)
from app.middleware.admission import AdaptiveAdmissionMiddleware


def controller(limit: int = 1, *, waiting: int = 0, timeout: float = 1.0, clock=None, min_limit: int = 1):
    settings = AdmissionSettings(min_limit=min_limit, max_limit=limit, queue_timeout=timeout, max_waiting=waiting)
    return AdmissionController(settings, clock=clock) if clock else AdmissionController(settings)


def test_apis_are_grouped_without_ids_and_ranked_by_criticality():
    assert group_of("GET", "/api/employees/12/qr") == "GET employees/{id}/qr"
    assert group_of("POST", "/api/checkpoint/identify/face") == "POST checkpoint/identify/face"
    assert group_of("GET", "/api/integrations/v1/attendance/feed") == "GET integrations/v1/attendance"
    assert group_of("GET", "/api/x/" + "a1" * 16) == "GET x/{id}"  # identificadores hexadecimales
    assert tier_of("POST checkpoint/identify/face") == Tier.CRITICAL
    assert tier_of("POST auth/login") == Tier.CRITICAL
    assert tier_of("POST auth/company") == Tier.CRITICAL  # elegir empresa también es iniciar sesión
    assert tier_of("GET employees") == Tier.NORMAL
    assert tier_of("GET admin/stats") == Tier.BACKGROUND
    # Tableros y estadísticas pesadas ceden su lugar al saturarse (auditoría de rendimiento).
    assert tier_of(group_of("GET", "/api/attendance/board")) == Tier.BACKGROUND
    assert tier_of(group_of("GET", "/api/admin/face-security")) == Tier.BACKGROUND
    assert tier_of(group_of("GET", "/api/attendance/sessions")) == Tier.NORMAL

    now = [0.0]

    async def scenario():
        c = controller(limit=10, clock=lambda: now[0])
        for n in range(MAX_GROUPS + 5):  # URLs inventadas no crecen la memoria sin límite
            assert await c.acquire(f"GET x{n}")
            c.release(f"GET x{n}", 0.001)
        assert len(c.groups) == MAX_GROUPS + 1
        # Ya sin lugar: una ruta crítica nueva comparte el grupo de SU nivel (sigue siendo crítica).
        assert await c.acquire("POST checkpoint/identify/nuevo")
        c.release("POST checkpoint/identify/nuevo", 0.001)
        assert c.groups[overflow_group(Tier.CRITICAL)].tier == Tier.CRITICAL
        assert c.groups[overflow_group(Tier.NORMAL)].demand == 5
        # Los inactivos se olvidan: mucho después, las rutas nuevas vuelven a tener su propio grupo.
        now[0] += 3600
        assert await c.acquire("GET employees")
        c.release("GET employees", 0.01)
        assert set(c.groups) == {"GET employees"}
        c.release("GET olvidado", 0.01)  # una petición larga de un grupo ya olvidado no falla
        return c

    asyncio.run(scenario())


def test_one_api_cannot_take_the_whole_queue():
    """Equidad: ni siquiera lo crítico ocupa más de la mitad de la fila (una avalancha de inicios de
    sesión no deja sin lugar al resto)."""

    async def scenario():
        c = controller(limit=1, waiting=4)
        assert await c.acquire("GET employees")
        logins = [asyncio.create_task(c.acquire("POST auth/login")) for _ in range(3)]
        await asyncio.sleep(0)
        assert logins[2].done() and logins[2].result() is False  # ya tenía su mitad de la fila
        other = asyncio.create_task(c.acquire("GET departments"))
        await asyncio.sleep(0)
        assert c.snapshot()["waiting"] == 3
        for group in ("GET employees", "POST auth/login", "POST auth/login"):
            c.release(group, 0.01)
            await asyncio.sleep(0)
        assert await other is True and all([await logins[0], await logins[1]])
        c.release("GET departments", 0.01)
        return c

    assert asyncio.run(scenario()).in_flight == 0


def test_abandoned_waiters_do_not_pile_up_in_the_queue():
    async def scenario():
        c = controller(limit=1, waiting=500)
        assert await c.acquire("GET employees")
        waiting = [asyncio.create_task(c.acquire(f"GET x{n % 50}")) for n in range(200)]
        await asyncio.sleep(0)
        for task in waiting:
            task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        assert c.snapshot()["waiting"] == 0
        assert len(c._waiting) <= 64  # se compactó: no guarda 200 entradas muertas
        c.release("GET employees", 0.01)
        assert await c.acquire("GET catalogs") is True

    asyncio.run(scenario())


def test_critical_first_then_the_most_demanded_apis():
    async def scenario():
        c = controller(limit=1, waiting=10)
        for _ in range(20):  # el catálogo tiene mucha más demanda reciente que los departamentos
            assert await c.acquire("GET catalogs")
            c.release("GET catalogs", 0.01)
        assert await c.acquire("GET employees")  # ocupa el único lugar
        order: list[str] = []

        async def wait(group: str) -> None:
            if await c.acquire(group):
                order.append(group)
                c.release(group, 0.01)

        groups = ("GET docs", "GET departments", "GET catalogs", "POST checkpoint/identify/face")
        tasks = [asyncio.create_task(wait(group)) for group in groups]
        await asyncio.sleep(0)
        assert c.snapshot()["waiting"] == 4
        c.release("GET employees", 0.01)
        await asyncio.gather(*tasks)
        return order

    assert asyncio.run(scenario()) == ["POST checkpoint/identify/face", "GET catalogs", "GET departments", "GET docs"]


def test_full_queue_sheds_the_least_important_first():
    async def scenario():
        c = controller(limit=1, waiting=1)
        assert await c.acquire("GET employees")
        low = asyncio.create_task(c.acquire("GET docs"))
        await asyncio.sleep(0)
        high = asyncio.create_task(c.acquire("POST auth/login"))  # desplaza a la de fondo
        await asyncio.sleep(0)
        assert await low is False
        assert await c.acquire("GET redoc") is False  # la fila está llena de algo más importante
        c.release("GET employees", 0.01)
        assert await high is True
        c.release("POST auth/login", 0.01)
        return c.snapshot()

    snapshot = asyncio.run(scenario())
    assert (snapshot["shed"], snapshot["waiting"], snapshot["in_flight"]) == (2, 0, 0)


def test_waiting_too_long_is_shed_and_leaves_the_queue():
    async def scenario():
        c = controller(limit=1, timeout=0.05)
        assert await c.acquire("GET employees")
        assert await c.acquire("GET departments") is False
        assert c.snapshot()["waiting"] == 0 and c.retry_after() >= 1
        c.release("GET employees", 0.01)
        assert await c.acquire("GET departments") is True  # el lugar no se perdió
        return c

    assert asyncio.run(scenario()).shed == 1


def test_client_leaving_while_waiting_does_not_leak_a_place():
    async def scenario():
        c = controller(limit=1)
        assert await c.acquire("GET employees")
        waiting = asyncio.create_task(c.acquire("GET departments"))
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        c.release("GET employees", 0.01)
        assert c.snapshot()["waiting"] == 0 and c.in_flight == 0
        assert await c.acquire("GET catalogs") is True

    asyncio.run(scenario())


def test_the_limit_follows_the_real_capacity():
    now = [0.0]

    async def serve(c: AdmissionController, group: str, seconds: float, times: int, busy: float = 0.0) -> None:
        """`times` respuestas de `seconds`, con `busy` (fracción del límite vigente) ocupada por otras
        peticiones en curso: con 0.85 la concurrencia está llena pero siempre queda un lugar."""
        held = 0
        for _ in range(times):
            while held < int(c.limit * busy):
                assert await c.acquire(group)
                held += 1
            while held > int(c.limit * busy):
                c.release(group, None)
                held -= 1
            assert await c.acquire(group)
            now[0] += 1.1  # una decisión por segundo como máximo
            c.release(group, seconds)
        for _ in range(held):
            c.release(group, None)

    async def scenario():
        c = controller(limit=50, clock=lambda: now[0], min_limit=5)
        await serve(c, "GET employees", 0.010, 20)  # su latencia normal: 10 ms
        assert c.limit == 50
        # Lenta pero con lugares de sobra: la causa no es nuestra concurrencia, no se recorta.
        await serve(c, "GET employees", 0.080, 10)
        assert c.limit == 50
        typical = c.groups["GET employees"].typical
        # Muy lenta Y con la concurrencia llena: se está formando fila, el límite baja. Lo típico no
        # aprende de la sobrecarga (solo con holgura).
        await serve(c, "GET employees", 0.300, 10, busy=0.85)
        congested = c.limit
        assert 5 <= congested < 50 and c.groups["GET employees"].typical == typical
        await serve(c, "GET employees", 0.010, 30)  # sana otra vez, sin demanda: el límite se queda
        assert c.limit == congested
        await serve(c, "GET employees", 0.010, 10, busy=0.85)  # sana y con demanda: sube
        return congested, c.limit

    congested, recovered = asyncio.run(scenario())
    assert recovered > congested


def test_saturated_api_answers_503_with_the_envelope_and_health_is_never_limited():
    async def slow_app(scope, receive, send):
        await asyncio.sleep(0.3)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = AdaptiveAdmissionMiddleware(slow_app, controller(limit=1, timeout=0.05))

    async def call(path="/api/employees"):
        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            return {"type": "http.request", "body": b""}

        await middleware({"type": "http", "path": path, "method": "GET", "headers": []}, receive, send)
        return messages

    async def scenario():
        return await asyncio.gather(call(), call(), call("/api/health/live"))

    first, second, health = asyncio.run(scenario())
    assert first[0]["status"] == 200 and health[0]["status"] == 200
    assert second[0]["status"] == 503
    assert (b"retry-after", b"1") in second[0]["headers"]
    body = json.loads(second[1]["body"])
    assert body["code"] == "SERVER_BUSY" and body["success"] is False and body["statusCode"] == 503


def test_only_successful_responses_measure_the_capacity():
    """Un 401 o un 503 inmediatos no dicen nada de la capacidad: no mueven la latencia típica."""
    statuses = iter((401, 503, 200))

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": next(statuses), "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    c = controller(limit=5)
    middleware = AdaptiveAdmissionMiddleware(app, c)

    async def call():
        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(_message):
            return None

        await middleware({"type": "http", "path": "/api/employees", "method": "GET", "headers": []}, receive, send)
        return c.groups["GET employees"].typical

    async def scenario():
        return [await call() for _ in range(3)]

    first, second, third = asyncio.run(scenario())
    assert first is None and second is None and third is not None


def test_admin_sees_the_adaptive_capacity_and_the_public_probe_does_not(
    client, admin_headers, company_headers, monkeypatch
):
    # Sin la demanda que dejaron otras pruebas (con más llamadas recientes sacarían a esta del top).
    monkeypatch.setattr(admission, "groups", {})
    client.get("/api/employees", headers=company_headers)
    server = client.get("/api/admin/errors/server", headers=admin_headers).json()["data"]
    capacity = server["admission"]
    assert capacity["limit"] >= capacity["bounds"][0] and capacity["waiting"] == 0
    assert any(item["api"] == "GET employees" and item["tier"] == "NORMAL" for item in capacity["top_demand"])
    assert "queue" in server["components"]["face_engine"] or "error" in server["components"]["face_engine"]
    public = client.get("/api/health/ready").json()["data"]
    assert "admission" not in public
    assert all(set(item) == {"status"} for item in public["components"].values())
    assert client.get("/api/admin/errors/server", headers=company_headers).status_code == 403


def test_each_tier_waits_only_its_share_of_the_queue_timeout():
    """Con la fila detenida, lo de fondo se rinde primero (un cuarto del tiempo), luego lo normal (la
    mitad) y lo crítico espera el tiempo completo: mejor un 503 rápido que reintentar a tiempo."""

    async def scenario():
        c = controller(limit=1, waiting=10, timeout=0.2)
        assert await c.acquire("GET employees")  # el único lugar, nunca se libera
        order: list[str] = []

        async def wait(group: str) -> None:
            assert await c.acquire(group) is False
            order.append(group)

        await asyncio.gather(*(wait(group) for group in ("POST auth/login", "GET departments", "GET docs")))
        return order, c

    order, c = asyncio.run(scenario())
    assert order == ["GET docs", "GET departments", "POST auth/login"]
    assert c.shed == 3 and c.snapshot()["waiting"] == 0


def test_a_request_that_would_not_make_it_in_time_is_rejected_on_arrival():
    """Con fila formada y un ritmo de salida conocido (1 lugar, 1 s por respuesta), lo de fondo no
    alcanzaría a pasar en su cuarto de segundo: 503 de inmediato, sin ocupar la fila. Lo crítico solo
    cuenta las de su nivel o más: sí espera su turno."""

    async def scenario():
        c = controller(limit=1, waiting=10, timeout=1.0)
        assert await c.acquire("GET catalogs")
        c.release("GET catalogs", 1.0)  # el ritmo real: una respuesta por segundo
        assert await c.acquire("GET employees")
        queued = asyncio.create_task(c.acquire("GET departments"))
        await asyncio.sleep(0)
        assert await c.acquire("GET docs") is False  # 2 s de espera estimada > 0.25 s
        login = asyncio.create_task(c.acquire("POST auth/login"))  # 1 s estimado: cabe en su tiempo
        await asyncio.sleep(0)
        waiting = c.snapshot()["waiting"]
        c.release("GET employees", 1.0)
        assert await login is True
        c.release("POST auth/login", 1.0)
        assert await queued is True
        c.release("GET departments", 1.0)
        return waiting, c

    waiting, c = asyncio.run(scenario())
    assert waiting == 2 and c.groups["GET docs"].shed == 1 and c.in_flight == 0


def test_a_place_granted_just_as_the_wait_expires_is_kept():
    """Carrera en el límite: el plazo vence y, en la misma vuelta del event loop, se libera un lugar
    que se le asigna. La petición pasa (no se pierde ese lugar ni se responde 503)."""

    async def scenario():
        c = controller(limit=1, timeout=0.05)
        assert await c.acquire("POST auth/login")
        waiting = asyncio.create_task(c.acquire("POST auth/login"))
        await asyncio.sleep(0)  # ya está en la fila; su plazo vence en 0.05 s
        loop = asyncio.get_running_loop()
        loop.call_at(loop.time() + 0.06, c.release, "POST auth/login", 0.01)  # justo después del plazo
        time.sleep(0.1)  # el event loop ocupado: ambos vencen en la misma vuelta (primero el plazo)
        return await waiting, c

    granted, c = asyncio.run(scenario())
    assert granted is True and c.in_flight == 1 and c.shed == 0


def test_a_client_leaving_just_as_it_was_admitted_gives_the_place_back():
    async def scenario():
        c = controller(limit=1)
        assert await c.acquire("GET employees")
        waiting = asyncio.create_task(c.acquire("GET departments"))
        await asyncio.sleep(0)
        waiting.cancel()  # el cliente se va...
        c.release("GET employees", 0.01)  # ...en el mismo instante en que se le da el lugar
        with pytest.raises(asyncio.CancelledError):
            await waiting
        return c

    c = asyncio.run(scenario())
    assert c.in_flight == 0 and c.snapshot()["waiting"] == 0
    assert asyncio.run(_admits(c, "GET catalogs"))  # el lugar quedó libre de verdad


async def _admits(c: AdmissionController, group: str) -> bool:
    return await c.acquire(group)


def test_a_client_leaving_just_as_it_was_displaced_gives_back_nothing():
    """El cliente de fondo se va en el mismo instante en que lo crítico lo desplaza de la fila llena:
    nunca tuvo lugar, así que no libera ninguno (el contador no queda negativo ni se regala un lugar)."""

    async def scenario():
        c = controller(limit=1, waiting=1)
        assert await c.acquire("GET employees")
        low = asyncio.create_task(c.acquire("GET docs"))
        await asyncio.sleep(0)
        low.cancel()  # el cliente se va...
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, c.release, "GET employees", 0.01)
        high = await c.acquire("POST auth/login")  # ...cuando lo crítico ya lo desplazó (sin ceder el turno)
        with pytest.raises(asyncio.CancelledError):
            await low
        return high, c

    high, c = asyncio.run(scenario())
    assert high is True and c.in_flight == 1 and c.snapshot()["waiting"] == 0


def test_saturation_warnings_do_not_flood_the_log(caplog):
    """Un aviso por cada 100 descartes (el primero, el 101...), no uno por petición rechazada."""
    c = controller(limit=1, timeout=0.01)
    middleware = AdaptiveAdmissionMiddleware(None, c)

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(_message):
        return None

    async def scenario():
        assert await c.acquire("GET employees")  # ocupa el único lugar
        for _ in range(3):
            await middleware({"type": "http", "path": "/api/docs", "method": "GET", "headers": []}, receive, send)

    with caplog.at_level(logging.WARNING, logger="app.middleware.admission"):
        asyncio.run(scenario())
    assert c.shed == 3
    assert [r.getMessage() for r in caplog.records] == ["API saturada: 1 peticiones descartadas (GET docs)"]
