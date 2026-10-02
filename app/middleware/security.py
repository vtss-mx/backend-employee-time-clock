"""Middlewares HTTP: cabeceras de seguridad y límite de tamaño del cuerpo (ASGI puro)."""

from fastapi import FastAPI, status
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.exceptions import error_response

_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, max_body: int) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = next((v for k, v in scope.get("headers", []) if k == b"content-length"), None)
        if content_length is not None:
            try:
                too_large = int(content_length) > self.max_body
            except ValueError:
                await error_response(status.HTTP_400_BAD_REQUEST, "BAD_REQUEST", "Content-Length inválido")(
                    scope, receive, send
                )
                return
            if too_large:
                response = error_response(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    "PAYLOAD_TOO_LARGE",
                    f"La solicitud excede el tamaño máximo ({settings.MAX_IMAGE_SIZE_MB} MB)",
                )
                await response(scope, receive, send)
                return

        is_api = not scope["path"].startswith(_DOCS_PATHS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
                if is_api:
                    # Las respuestas de la API pueden contener datos personales: no cachear.
                    headers.setdefault("Cache-Control", "no-store")
                    headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                if settings.is_production:
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def register_security_middlewares(app: FastAPI) -> None:
    # Hasta 5 imágenes por solicitud (registro) + margen para campos multipart.
    app.add_middleware(SecurityMiddleware, max_body=settings.max_image_bytes * 5 + 256 * 1024)
