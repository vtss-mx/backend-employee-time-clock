from typing import Any


class FaceValidationError(Exception):
    """La imagen no cumple los requisitos para generar un embedding confiable."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
