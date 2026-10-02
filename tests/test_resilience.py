"""Tolerancia a fallas: degradación controlada en lugar de errores 500."""

from sqlalchemy.exc import OperationalError

from app import dependencies
from app.dependencies import get_pipeline
from app.facial_recognition import FaceEngineUnavailable
from app.main import app
from tests.conftest import approved_employee


def test_health_probes(client):
    assert client.get("/api/health/live").json()["data"] == {"status": "ok"}
    ready = client.get("/api/health/ready")
    assert ready.status_code == 200
    assert ready.json()["data"]["components"]["database"]["status"] == "ok"


def test_face_engine_down_returns_503_but_qr_and_admin_keep_working(client, company_headers):
    headers = approved_employee(client, company_headers)

    def broken_pipeline():
        try:
            raise FaceEngineUnavailable("modelo corrupto")
        except FaceEngineUnavailable as exc:
            from app.core.exceptions import ServiceUnavailableError

            raise ServiceUnavailableError(
                "El reconocimiento facial no está disponible temporalmente. Puedes identificarte con QR.",
                code="FACE_SERVICE_UNAVAILABLE",
            ) from exc

    app.dependency_overrides[get_pipeline] = broken_pipeline
    response = client.post("/api/face/check", files={"image": ("c.jpg", b"face:juan", "image/jpeg")}, headers=headers)
    assert response.status_code == 503
    assert response.json()["code"] == "FACE_SERVICE_UNAVAILABLE"
    assert "Retry-After" in response.headers and response.json()["traceId"]
    # El resto del sistema sigue operando.
    assert client.get("/api/employees", headers=company_headers).status_code == 200
    assert client.post("/api/verification/qr", json={"qr_content": "x"}, headers=headers).status_code == 200


def test_database_outage_returns_503(client, company_headers, monkeypatch):
    from app.repositories.employee_repository import EmployeeRepository

    def fail(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(EmployeeRepository, "search", fail)
    response = client.get("/api/employees", headers=company_headers)
    assert response.status_code == 503
    assert response.json()["code"] == "DATABASE_UNAVAILABLE"


def test_worker_pool_fifo_and_backpressure():
    """1 worker: el segundo espera en cola; con la cola llena se rechaza de inmediato."""
    import threading
    import time

    from app.facial_recognition.worker_pool import QueueFullError, QueueTimeoutError, WorkerPool

    pool = WorkerPool(lambda i: f"worker-{i}", size=1, max_waiting=1, wait_timeout=2)
    order: list[str] = []
    started = threading.Event()

    def hold(name: str, seconds: float) -> None:
        with pool.lease():
            order.append(name)
            started.set()
            time.sleep(seconds)

    t1 = threading.Thread(target=hold, args=("a", 0.3))
    t1.start()
    started.wait()
    t2 = threading.Thread(target=hold, args=("b", 0))
    t2.start()
    time.sleep(0.05)
    assert pool.stats().waiting == 1
    try:
        with pool.lease():
            raise AssertionError("debió rechazarse: cola llena")
    except QueueFullError:
        pass
    t1.join()
    t2.join()
    assert order == ["a", "b"] and pool.stats().processed == 2 and pool.stats().rejected == 1

    slow = WorkerPool(lambda i: i, size=1, max_waiting=5, wait_timeout=0.05)
    with slow.lease():
        try:
            with slow.lease():
                raise AssertionError("debió expirar")
        except QueueTimeoutError:
            pass
    with slow.lease():  # la fila sigue avanzando tras un ticket expirado
        pass


def test_face_busy_returns_503(client, company_headers, monkeypatch):
    from contextlib import contextmanager

    from app.facial_recognition import QueueFullError

    headers = approved_employee(client, company_headers)
    app.dependency_overrides.pop(get_pipeline, None)  # usar la dependencia real

    @contextmanager
    def full():
        raise QueueFullError("llena")
        yield  # pragma: no cover

    monkeypatch.setattr(dependencies, "lease_pipeline", full)
    response = client.post("/api/face/check", files={"image": ("c.jpg", b"face:juan", "image/jpeg")}, headers=headers)
    assert response.status_code == 503 and response.json()["code"] == "FACE_SERVICE_BUSY"
    assert response.headers["Retry-After"] == "3"


def test_request_id_is_propagated(client):
    response = client.get("/api/health/live", headers={"X-Request-ID": "abc12345-test"})
    assert response.headers["X-Request-ID"] == "abc12345-test"
    response = client.post("/api/auth/login", json={"email": "x@y.com", "password": "bad"})
    assert response.json()["traceId"] == response.headers["X-Request-ID"]


def test_admission_control_rejects_with_envelope_when_saturated():
    import asyncio
    import json

    from app.middleware.concurrency import ConcurrencyLimitMiddleware

    async def slow_app(scope, receive, send):
        await asyncio.sleep(0.3)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = ConcurrencyLimitMiddleware(slow_app, max_concurrent=1, queue_timeout=0.05)

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
    assert first[0]["status"] == 200 and health[0]["status"] == 200  # salud nunca se limita
    assert second[0]["status"] == 503
    body = json.loads(second[1]["body"])
    assert body["code"] == "SERVER_BUSY" and body["success"] is False and body["statusCode"] == 503


def test_password_hashing_is_bounded(client, monkeypatch):
    import threading

    from app.core import passwords as security

    busy = threading.BoundedSemaphore(1)
    busy.acquire()  # todos los "núcleos" ocupados
    monkeypatch.setattr(security, "_hash_slots", busy)
    monkeypatch.setattr(security.settings, "PASSWORD_HASH_WAIT_SECONDS", 0.05)
    response = client.post("/api/auth/login", json={"email": "admin@test.com", "password": "Whatever123"})
    assert response.status_code == 503 and response.json()["code"] == "AUTH_BUSY"
    assert response.headers["Retry-After"] == "2"
