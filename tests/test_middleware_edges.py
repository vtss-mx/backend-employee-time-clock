"""Middlewares en sus casos límite: límite de tamaño y cabeceras (security) y limitador en memoria
(rate_limit)."""

import asyncio
import json
from types import SimpleNamespace

from app.core.config import settings
from app.middleware import rate_limit
from app.middleware.security import SecurityMiddleware


def _asgi_call(middleware, headers: list[tuple[bytes, bytes]], incoming: list[dict]) -> list[dict]:
    """Llama al middleware como lo haría el servidor y devuelve lo que respondió."""
    sent: list[dict] = []
    messages = iter(incoming)

    async def receive() -> dict:
        return next(messages)

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "path": "/api/employees", "method": "POST", "headers": headers}
    asyncio.run(middleware(scope, receive, send))
    return sent


def _unreachable_app(*_args) -> None:
    raise AssertionError("la petición no debió llegar a la API")


def test_invalid_content_length_is_rejected_with_the_envelope():
    """Un Content-Length que no es un número no llega a la API: 400 con el contrato de siempre."""
    sent = _asgi_call(SecurityMiddleware(_unreachable_app, max_body=1024), [(b"content-length", b"diez")], [])
    assert sent[0]["status"] == 400
    body = json.loads(sent[1]["body"])
    assert (body["code"], body["success"], body["message"]) == ("BAD_REQUEST", False, "Content-Length inválido")


def test_a_client_disconnect_is_forwarded_and_not_counted_as_body():
    """Sin Content-Length se cuenta lo recibido; un aviso de desconexión no es cuerpo: llega intacto a
    la API (que deja de esperar) y no dispara el 413."""
    seen: list[dict] = []

    async def api(_scope, receive, send) -> None:
        while (message := await receive())["type"] == "http.request":
            seen.append(message)
        seen.append(message)
        await send({"type": "http.response.start", "status": 499, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    incoming = [{"type": "http.request", "body": b"x" * 10, "more_body": True}, {"type": "http.disconnect"}]
    sent = _asgi_call(SecurityMiddleware(api, max_body=16), [], incoming)
    assert [m["type"] for m in seen] == ["http.request", "http.disconnect"]
    assert sent[0]["status"] == 499


def test_production_forces_https_with_hsts(client, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "Production")
    headers = client.get("/api/health/live").headers
    assert headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert "Strict-Transport-Security" not in client.get("/api/health/live").headers  # sin HTTPS en la LAN


# ---------------------------------------------------------------- limitador en memoria


def _memory_limiter(monkeypatch) -> tuple[rate_limit.InMemoryRateLimiter, list[float]]:
    """Limitador con un reloj controlado por la prueba (sin esperar de verdad)."""
    now = [1000.0]
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return rate_limit.InMemoryRateLimiter(), now


def test_the_sliding_window_frees_attempts_as_they_age(monkeypatch):
    limiter, now = _memory_limiter(monkeypatch)
    assert limiter.hit("login:ana", 2, 60) is None
    now[0] += 30
    assert limiter.hit("login:ana", 2, 60) is None
    assert limiter.hit("login:ana", 2, 60) == 30  # espera a que salga el primero
    now[0] += 31  # el primero ya salió de la ventana; el segundo sigue
    assert limiter.hit("login:ana", 2, 60) is None
    assert limiter.hit("login:ana", 2, 60) == 29


def test_idle_keys_are_forgotten_so_memory_stays_bounded(monkeypatch):
    """Cada IP o correo que alguna vez lo intentó no se guarda para siempre: cada 5 min se olvidan
    las claves sin intentos recientes (las activas se conservan)."""
    limiter, now = _memory_limiter(monkeypatch)
    for n in range(50):
        limiter.hit(f"login:ip:{n}", 10, 60)
    now[0] += 299
    limiter.hit("login:ip:activa", 10, 60)
    assert len(limiter._hits) == 51  # aún no toca limpiar
    now[0] += 2
    limiter.hit("login:ip:nueva", 10, 60)
    assert set(limiter._hits) == {"login:ip:activa", "login:ip:nueva"}
