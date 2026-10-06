"""Foto de perfil, reglas puras (`app/services/avatar_image.py`): formatos por su contenido, defensa contra
bombas de descompresión, orientación EXIF, metadatos FUERA (GPS incluido), colores en sRGB, recorte y tamaños."""

import io

import pytest
from PIL import Image

from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.services.avatar_image import CropBox, render
from tests.avatar_support import BLUE, GREEN, RED, SRGB, encode, halves, photo, png_header


def _close(pixel: tuple[int, ...], color: tuple[int, int, int], tolerance: int = 40) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], color, strict=True))


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _rejected(data: bytes, crop: CropBox | None = None) -> UnprocessableError:
    with pytest.raises(UnprocessableError) as caught:
        render(data, crop)
    return caught.value


def test_a_phone_photo_is_oriented_cropped_resized_and_stripped_of_every_metadata():
    """12 MP de un teléfono girado (EXIF 6), con marca, ubicación GPS y perfil de color: sale derecha, cuadrada,
    en 512 y 96 px WebP, y sin un solo metadato (ni EXIF, ni GPS, ni perfil, ni XMP)."""
    data = encode(halves(4000, 3000), "JPEG", orientation=6, gps=True, icc_profile=SRGB, quality=85)
    assert b"Apple" in data
    sizes = render(data, None)
    assert sorted(sizes) == [96, 512]
    for side, webp in sizes.items():
        assert webp[:4] == b"RIFF" and webp[8:12] == b"WEBP"
        assert b"Apple" not in webp and b"EXIF" not in webp and b"ICCP" not in webp and b"XMP " not in webp
        image = _open(webp)
        assert image.size == (side, side) and image.mode == "RGB"
        assert not {"exif", "icc_profile", "xmp"} & set(image.info) and dict(image.getexif()) == {}
    large = _open(sizes[512])
    # Girada 90° a la derecha: la mitad izquierda (roja) queda arriba y la derecha (azul) abajo.
    assert _close(large.getpixel((256, 20)), RED) and _close(large.getpixel((256, 490)), BLUE)


def test_the_chosen_crop_is_kept():
    data = photo(1000, 500)  # izquierda roja (0-499), derecha azul (500-999)
    left = _open(render(data, CropBox(0, 0, 500))[96])
    right = _open(render(data, CropBox(500, 0, 500))[96])
    assert _close(left.getpixel((48, 48)), RED) and _close(right.getpixel((48, 48)), BLUE)


def test_a_crop_on_a_rotated_photo_uses_the_oriented_pixels():
    data = encode(halves(1200, 900, RED, GREEN), "JPEG", orientation=6)  # se ve de 900 × 1200
    bottom = _open(render(data, CropBox(0, 1200 - 900, 900))[96])
    assert _close(bottom.getpixel((48, 90)), GREEN)


@pytest.mark.parametrize(
    ("image", "fmt", "mode"),
    [
        (Image.new("RGBA", (300, 300), (10, 120, 200, 128)), "PNG", "RGBA"),  # transparencia
        (Image.new("P", (300, 300), 3), "PNG", "RGB"),  # paleta sin transparencia
        (Image.new("L", (300, 300), 90), "JPEG", "RGB"),  # gris
        (Image.new("CMYK", (300, 300), (0, 80, 80, 0)), "JPEG", "RGB"),  # imprenta
        (Image.new("RGB", (300, 300), GREEN), "WEBP", "RGB"),
    ],
)
def test_every_accepted_kind_ends_in_rgb_or_rgba(image, fmt, mode):
    webp = render(encode(image, fmt), None)[512]
    assert _open(webp).mode == mode


def test_a_palette_with_transparency_keeps_it():
    image = Image.new("P", (300, 300), 0)
    image.info["transparency"] = 0
    assert _open(render(encode(image, "PNG", transparency=0), None)[96]).mode == "RGBA"


def test_a_png_with_its_orientation_is_turned_too():
    data = encode(halves(400, 300), "PNG", orientation=6)
    assert _close(_open(render(data, None)[512]).getpixel((256, 20)), RED)


def test_a_color_profile_that_cannot_be_read_is_ignored():
    data = encode(halves(400, 300), "JPEG", icc_profile=b"no-es-un-perfil")
    assert _open(render(data, None)[96]).mode == "RGB"


def test_a_profile_of_another_kind_on_a_gray_photo_is_left_alone():
    data = encode(Image.new("L", (300, 300), 120), "JPEG", icc_profile=SRGB)
    assert _open(render(data, None)[96]).mode == "RGB"


def test_a_small_photo_is_decoded_as_is():
    """Lado de 300 px: no hace falta reducir al decodificar (se amplía a 512)."""
    assert _open(render(photo(400, 300), None)[512]).size == (512, 512)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "No se recibió ninguna imagen"),
        (b"no soy una imagen", "JPG, PNG o WEBP"),
        (encode(Image.new("RGB", (300, 300)), "GIF"), "JPG, PNG o WEBP"),  # otro formato real: tampoco
        (png_header(8000, 6000), "megapíxeles"),  # 48 MP: se rechaza sin decodificar
        (png_header(20_000, 20_000), "megapíxeles"),  # bomba: Pillow ni la abre
        (photo(800, 600)[:3000], "dañada o incompleta"),  # JPEG truncado
        (encode(halves(400, 300), "PNG", orientation=6)[:400], "dañada o incompleta"),  # PNG truncado
        (photo(100, 300), "al menos 128 px"),
    ],
)
def test_what_is_not_a_valid_photo_is_rejected(data, message):
    error = _rejected(data)
    assert error.code == "AVATAR_INVALID" and error.field == "file" and message in error.message


@pytest.mark.parametrize(
    "crop",
    [CropBox(0, 0, 100), CropBox(500, 0, 600), CropBox(0, 200, 500), CropBox(10_000, 0, 200)],
)
def test_a_crop_outside_the_photo_or_too_small_is_rejected(crop):
    error = _rejected(photo(1000, 600), crop)
    assert error.code == "AVATAR_CROP_INVALID" and error.field == "crop" and "1000 × 600" in error.message


def test_the_limits_come_from_the_settings(monkeypatch):
    monkeypatch.setattr(settings, "AVATAR_MIN_SIDE_PX", 64)
    assert sorted(render(photo(100, 80), None)) == [96, 512]
    monkeypatch.setattr(settings, "AVATAR_MAX_MEGAPIXELS", 0.01)
    assert "0.01 megapíxeles" in _rejected(photo(400, 300)).message
