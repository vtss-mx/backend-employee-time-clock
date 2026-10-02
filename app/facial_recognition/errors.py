from typing import Any


class FaceValidationError(Exception):
    """La imagen no cumple los requisitos para generar un embedding confiable.

    Solo lleva el código (y sus datos): el motor facial corre en otros procesos, sin base de datos,
    y el mensaje para la persona lo pone la API con el catálogo `face_errors`.
    """

    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details
