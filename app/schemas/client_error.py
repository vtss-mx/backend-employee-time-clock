"""Contrato de `POST /api/client-errors`: una falla de la aplicación web que reporta el navegador.

Es una ruta pública (una pantalla puede romperse en el inicio de sesión), así que todo va acotado:
el cuerpo completo (`MAX_BODY_BYTES`), cada texto y los campos permitidos (otro campo es un 422).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Tope del cuerpo JSON del reporte: holgado para un stack trace de 8 000 caracteres y lo demás.
MAX_BODY_BYTES = 16 * 1024

ClientErrorKind = Literal["CRASH", "UNHANDLED", "CONFIG"]


class ClientErrorIn(BaseModel):
    """Lo que la aplicación web cuenta de su falla (sin datos de la persona: solo la falla y dónde)."""

    model_config = ConfigDict(extra="forbid")

    kind: ClientErrorKind = Field(
        description=(
            "CRASH: una pantalla se rompió (ErrorBoundary) · UNHANDLED: error inesperado sin capturar · "
            "CONFIG: configuración de la plataforma que la persona no puede arreglar (p. ej. una API de "
            "Google Maps sin habilitar)"
        )
    )
    message: str = Field(
        min_length=1, max_length=1000, description="`Tipo: mensaje` del error (p. ej. `TypeError: ...`)"
    )
    stack: str | None = Field(default=None, max_length=8000, description="Stack trace de JavaScript")
    path: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^/[^?#\s]*$",
        description="Ruta de la pantalla (`location.pathname`), sin query ni fragmento",
    )
    component: str | None = Field(default=None, max_length=500, description="Componente o módulo donde ocurrió")
    detail: str | None = Field(
        default=None, max_length=500, description="Dato adicional (p. ej. la pila de componentes)"
    )
    app_version: str | None = Field(default=None, max_length=64, description="Compilación de la app (`buildId`)")
