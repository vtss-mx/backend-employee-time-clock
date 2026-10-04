"""Contexto literal de un error HTTP: quién, qué pidió (método, URL, encabezados, cuerpo) y qué se le
respondió (estado y cuerpo), para que el ADMIN lo vea tal cual en "Errores del sistema".

Todo es literal salvo dos cosas, a propósito:
- **Secretos**: un campo cuyo nombre es de contraseña, token, llave, cookie o firma se guarda como
  «[oculto]» (también en la URL). Los encabezados `Authorization` y `Cookie` nunca se copian.
- **Archivos** (las fotos del rostro): de cada uno solo su campo, nombre, tipo y tamaño; nunca los
  bytes. Así un error de un registro facial no deja biometría en la bitácora.

La copia del cuerpo se hace mientras se lee (sin leerlo dos veces) y está acotada
(`CAPTURE_LIMIT`); un cuerpo multipart se resume parte por parte sin guardar sus archivos.
"""

import json
import re
from typing import Any
from urllib.parse import parse_qsl

#: Bytes del cuerpo (de la petición o de la respuesta de error) que se guardan como máximo.
CAPTURE_LIMIT = 64 * 1024
#: Texto de un campo de formulario que se guarda como máximo.
_FIELD_LIMIT = 2_000
_HIDDEN = "[oculto]"
_SECRET = re.compile(
    r"pass|contrase|token|secret|api[_-]?key|authorization|cookie|signature|private|credential|proof", re.I
)
#: Encabezados que sí se copian (nunca Authorization ni Cookie).
_HEADERS: tuple[str, ...] = ("user-agent", "content-type", "content-length", "origin", "referer", "accept-language")
_HEADERS += ("x-forwarded-for", "x-real-ip", "x-request-id")


def redact(value: Any) -> Any:
    """El valor tal cual, salvo los campos con nombre de secreto (en cualquier nivel)."""
    if isinstance(value, dict):
        return {key: _HIDDEN if _SECRET.search(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _pairs(raw: str) -> dict[str, Any]:
    """`a=1&b=2&b=3` → {"a": "1", "b": ["2", "3"]} (con los secretos ocultos)."""
    found: dict[str, Any] = {}
    for key, value in parse_qsl(raw, keep_blank_values=True):
        if key in found:
            previous = found[key]
            found[key] = [*previous, value] if isinstance(previous, list) else [previous, value]
        else:
            found[key] = value
    return redact(found)


def query_of(raw: bytes) -> dict[str, Any] | None:
    return _pairs(raw.decode("latin-1")) if raw else None


def headers_of(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {k.decode("latin-1"): v.decode("latin-1") for k, v in headers if k.decode("latin-1").lower() in _HEADERS}


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def parse_body(content_type: str, raw: bytes) -> Any:
    """El cuerpo legible: JSON o formulario como datos (con secretos ocultos), texto como texto."""
    if not raw:
        return None
    if "json" in content_type:
        try:
            return redact(json.loads(raw))
        except ValueError:
            return _text(raw)
    if "x-www-form-urlencoded" in content_type:
        return _pairs(_text(raw))
    if content_type.startswith("text/") or not content_type:
        return _text(raw)
    return f"[{len(raw)} bytes de {content_type}]"


class _Multipart:
    """Resume un cuerpo multipart conforme llega: campos de texto con su valor; archivos solo con su
    nombre, tipo y tamaño (sus bytes se cuentan y se descartan)."""

    def __init__(self, boundary: bytes) -> None:
        self.delimiter = b"\r\n--" + boundary
        self.pending = b"\r\n"  # el primer delimitador no lleva salto de línea antes
        self.state = "preamble"
        self.parts: list[dict[str, Any]] = []
        self.part: dict[str, Any] = {}
        self.text = b""

    def feed(self, chunk: bytes) -> None:
        # Lo pendiente se consume aquí; cada estado que se detiene guarda de nuevo lo que le falta.
        data, self.pending = self.pending + chunk, b""
        while data and self.state != "done":
            data = getattr(self, f"_{self.state}")(data)
            if data is None:
                return

    def _preamble(self, data: bytes) -> bytes | None:
        index = data.find(self.delimiter)
        if index < 0:
            self.pending = data[-len(self.delimiter) :]
            return None
        return self._after_delimiter(data[index + len(self.delimiter) :])

    def _after_delimiter(self, data: bytes) -> bytes | None:
        if len(data) < 2:
            self.pending = self.delimiter + data
            self.state = "preamble"
            return None
        self.state = "done" if data.startswith(b"--") else "headers"
        return data[2:]

    def _headers(self, data: bytes) -> bytes | None:
        end = data.find(b"\r\n\r\n")
        if end < 0:
            self.pending = data
            if len(data) > 8_192:  # encabezados imposibles: se deja de resumir
                self.state = "done"
            return None
        headers = _text(data[:end])
        name = re.search(r'name="([^"]*)"', headers)
        filename = re.search(r'filename="([^"]*)"', headers)
        kind = re.search(r"content-type:\s*([^\r\n]+)", headers, re.I)
        self.part = {"name": name.group(1) if name else ""}
        if filename:
            self.part |= {
                "filename": filename.group(1),
                "content_type": kind.group(1).strip() if kind else None,
                "size": 0,
            }
        self.text = b""
        self.state = "body"
        return data[end + 4 :]

    def _body(self, data: bytes) -> bytes | None:
        index = data.find(self.delimiter)
        content, rest = (data, None) if index < 0 else (data[:index], data[index + len(self.delimiter) :])
        if rest is None:  # puede venir medio delimitador al final: se guarda para el siguiente pedazo
            keep = len(self.delimiter)
            content, self.pending = data[:-keep], data[-keep:]
        if "filename" in self.part:
            self.part["size"] += len(content)
        else:
            self.text = (self.text + content)[:_FIELD_LIMIT]
        if rest is None:
            return None
        self._close_part()
        return self._after_delimiter(rest)

    def _close_part(self) -> None:
        if "filename" not in self.part:
            name = str(self.part["name"])
            self.part["value"] = _HIDDEN if _SECRET.search(name) else _text(self.text)
        self.parts.append(self.part)

    def summary(self) -> list[dict[str, Any]]:
        if self.state == "body":  # cuerpo cortado: la última parte va con lo que se alcanzó a ver
            self._close_part()
            self.state = "done"
        return self.parts


class BodyCapture:
    """Copia acotada del cuerpo de una petición mientras la aplicación lo lee."""

    def __init__(self, content_type: str) -> None:
        self.content_type = content_type.lower()
        self.total = 0
        self.raw = b""
        boundary = re.search(r"boundary=\"?([^\";]+)\"?", content_type)
        self.multipart = (
            _Multipart(boundary.group(1).encode()) if "multipart/" in self.content_type and boundary else None
        )

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.multipart is not None:
            self.multipart.feed(chunk)
        elif len(self.raw) < CAPTURE_LIMIT:
            self.raw += chunk[: CAPTURE_LIMIT - len(self.raw)]

    @property
    def truncated(self) -> bool:
        return self.multipart is None and self.total > len(self.raw)

    def value(self) -> Any:
        if self.multipart is not None:
            return self.multipart.summary()
        if self.truncated:  # cortado: se guarda el texto tal cual (no se puede leer como JSON completo)
            return _text(self.raw)
        return parse_body(self.content_type, self.raw)
