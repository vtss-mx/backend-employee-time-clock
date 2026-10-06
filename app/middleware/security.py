"""Middlewares HTTP: cabeceras de seguridad y límite de tamaño del cuerpo (ASGI puro)."""

from fastapi import FastAPI, status
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.exceptions import BodyTooLargeError, error_response
from app.i18n import Megabytes, Text, t

_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


def max_request_bytes() -> int:
    """Límite general del cuerpo de una petición: cinco imágenes del tamaño máximo (MAX_IMAGE_SIZE_MB) más 256 KB para
    los campos del formulario. Cabe con holgura el registro facial más grande (decisión del dueño, 2026-10-06): 36 fotos
    de 640 px a calidad 92 (≈ 50 KB cada una, ≈ 1.8 MB) más la prueba de vida (destello, movimientos y la ráfaga:
    ≈ 1 MB) son ≈ 3 MB, diez veces menos que el límite. Una sola imagen más grande que MAX_IMAGE_SIZE_MB la rechaza su
    lectura (413 IMAGE_FILE_TOO_LARGE) y más fotos de FACE_ENROLL_MAX_PHOTOS, 422 TOO_MANY_IMAGES. Un documento de la
    empresa (`COMPANY_DOCUMENT_MAX_MB`, 20 MB) cabe en el mismo límite; si se configurara más grande, el límite crece
    con él (su lectura lo rechaza con 413 DOCUMENT_TOO_LARGE y el gateway corta antes, en su client_max_body_size)."""
    document = int(settings.COMPANY_DOCUMENT_MAX_MB * 1024 * 1024)
    return max(settings.max_image_bytes * 5, document) + 256 * 1024


def _too_large(limit: int) -> Text:
    """El mensaje del cuerpo que pasa del límite general, con ESE límite (se traduce al responder, en el idioma de la
    petición)."""
    return Text("REQUEST_TOO_LARGE", {"size": Megabytes(limit)})


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
                await error_response(status.HTTP_400_BAD_REQUEST, "BAD_REQUEST", t("CONTENT_LENGTH_INVALID"))(
                    scope, receive, send
                )
                return
            if too_large:
                response = error_response(
                    status.HTTP_413_CONTENT_TOO_LARGE, "PAYLOAD_TOO_LARGE", str(_too_large(self.max_body))
                )
                await response(scope, receive, send)
                return

        # Sin Content-Length (envío por partes): se cuenta lo recibido y se corta al pasar el límite.
        # El error llega a quien lee el cuerpo y sale como 413 con el sobre de siempre.
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise BodyTooLargeError(_too_large(self.max_body))
            return message

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

        await self.app(scope, limited_receive, send_with_headers)


def register_security_middlewares(app: FastAPI) -> None:
    app.add_middleware(SecurityMiddleware, max_body=max_request_bytes())
