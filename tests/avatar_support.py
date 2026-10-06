"""Fotos de prueba para la foto de perfil (generadas con Pillow en memoria) y su subida por la API."""

import base64
import io
import os
import struct
import zlib

from PIL import ExifTags, Image, ImageCms

RED = (220, 30, 30)
BLUE = (30, 30, 220)
GREEN = (30, 200, 30)
#: Perfil de color sRGB (el que se convierte sin cambiar los colores).
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def halves(
    width: int, height: int, left: tuple[int, int, int] = RED, right: tuple[int, int, int] = BLUE
) -> Image.Image:
    """Mitad izquierda de un color y derecha de otro: así se ve si se giró o recortó bien."""
    image = Image.new("RGB", (width, height), left)
    image.paste(right, (width // 2, 0, width, height))
    return image


def encode(
    image: Image.Image, fmt: str = "JPEG", *, orientation: int | None = None, gps: bool = False, **params: object
) -> bytes:
    """La imagen en `fmt`, con orientación EXIF y datos personales (marca y ubicación GPS) si se piden."""
    exif = Image.Exif()
    if orientation:
        exif[ExifTags.Base.Orientation] = orientation
    if gps:
        exif[ExifTags.Base.Make] = "Apple"
        exif[ExifTags.IFD.GPSInfo] = {ExifTags.GPS.GPSLatitudeRef: "N", ExifTags.GPS.GPSLatitude: (29.0, 4.0, 52.0)}
    if orientation or gps:
        params["exif"] = exif.tobytes()
    buffer = io.BytesIO()
    image.save(buffer, fmt, **params)
    return buffer.getvalue()


def photo(width: int = 800, height: int = 600, fmt: str = "JPEG", **params: object) -> bytes:
    return encode(halves(width, height), fmt, **params)


def noise(width: int, height: int) -> bytes:
    """Una foto de ruido (no se comprime): para pasar de un tamaño en bytes."""
    return encode(Image.frombytes("RGB", (width, height), os.urandom(width * height * 3)), "JPEG", quality=95)


def png_header(width: int, height: int) -> bytes:
    """Un PNG que dice medir `width` × `height` sin traer sus píxeles (para las "bombas" de descompresión)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")


def upload(client, headers, data: bytes | None = None, *, crop: dict[str, int] | None = None, name: str = "foto.jpg"):
    """`PUT /users/me/avatar` con la foto (por omisión una de 800 × 600) y, si se da, el recorte."""
    files = {"file": (name, photo() if data is None else data, "image/jpeg")}
    form = {key: str(value) for key, value in (crop or {}).items()}
    return client.put("/api/users/me/avatar", files=files, data=form, headers=headers)


def me(client, headers) -> dict:
    return client.get("/api/users/me", headers=headers).json()["data"]


def fetch(client, headers, url: str, size: int = 96, **kwargs):
    """La foto de la ruta versionada (`user.avatar`) en un tamaño."""
    return client.get(f"/api{url}&size={size}", headers=headers, **kwargs)


def decoded(response) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(response.json()["data"]["data"])))
