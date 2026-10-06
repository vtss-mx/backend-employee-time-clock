"""Foto de perfil: de lo que sube la persona a los tamaños que se guardan (reglas puras: sin BD ni red).

Decisiones (README, "Foto de perfil"):

1. **Solo JPEG, PNG o WEBP reconocidos por su CONTENIDO** (Pillow abre solo con esos decodificadores; lo que
   declara el navegador no cuenta). HEIC no: Pillow no lo decodifica sin un complemento que la plataforma no
   instala (el navegador del iPhone ya entrega JPEG al elegir una foto).
2. **Defensa contra "bombas" de descompresión**: antes de decodificar se miran las dimensiones del encabezado
   (`AVATAR_MAX_MEGAPIXELS`); un JPEG grande se decodifica ya reducido (`draft`: 1/2, 1/4 u 1/8) a lo que el
   recorte necesita para 512 px. Una foto de 12 MP del teléfono cuesta así una fracción de memoria y CPU.
3. **Orientación EXIF aplicada y TODOS los metadatos fuera** (EXIF con la ubicación GPS, XMP, IPTC,
   comentarios, perfil de color): la imagen se vuelve a codificar solo desde sus píxeles. Un perfil de color
   RGB (p. ej. Display P3 del iPhone) se convierte antes a sRGB para que los colores no cambien.
4. **Recorte cuadrado**: el que eligió la persona en la app (validado: dentro de la imagen ya orientada y con
   lado ≥ `AVATAR_MIN_SIDE_PX`) o, sin recorte, el cuadrado del centro.
5. **Tamaños 512 y 96 px en WebP** (`AVATAR_WEBP_QUALITY`), con transparencia si la imagen la tenía.

Toda falla es un 4xx con código estable (`AVATAR_INVALID`, `AVATAR_CROP_INVALID`): un archivo que no es una
imagen válida es un resultado normal, no algo que el ADMIN deba corregir.
"""

import io
import math
from dataclasses import dataclass
from functools import cache
from typing import Final

from PIL import ExifTags, Image, ImageCms, ImageOps, UnidentifiedImageError

from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.i18n import Params
from app.schemas.avatar import AVATAR_SIZES

#: Tipo de lo que se guarda (y se entrega al leer).
CONTENT_TYPE: Final = "image/webp"
#: Decodificadores que se aceptan (un JPEG del iPhone con varias imágenes abre como MPO: es un JPEG).
_FORMATS: Final = ("JPEG", "PNG", "WEBP")
_JPEG: Final = ("JPEG", "MPO")
#: Orientaciones EXIF que giran la imagen 90° (el ancho y el alto se intercambian).
_SWAPPED: Final = frozenset({5, 6, 7, 8})


@dataclass(frozen=True)
class CropBox:
    """Cuadrado a conservar, en píxeles de la imagen YA orientada (como la muestra el navegador)."""

    x: int
    y: int
    size: int


def _invalid(key: str, params: Params | None = None) -> UnprocessableError:
    """422 `AVATAR_INVALID` con el mensaje de `key` (catálogo de mensajes)."""
    return UnprocessableError(code="AVATAR_INVALID", key=key, params=params, field="file")


def render(data: bytes, crop: CropBox | None) -> dict[int, bytes]:
    """La foto lista para guardarse: {lado en px: WebP sin metadatos} para cada tamaño de `AVATAR_SIZES`."""
    image = _open(data)
    try:  # decodificar puede descubrir una imagen truncada o dañada después del encabezado
        width, height = _oriented_size(image)
        box = _crop_box(crop, width, height)
        square = _square(image, box, width)
    except (OSError, ValueError) as exc:
        raise _invalid("AVATAR_DAMAGED") from exc
    large = square.resize((AVATAR_SIZES[0], AVATAR_SIZES[0]), Image.Resampling.LANCZOS, reducing_gap=3.0)
    sizes = {AVATAR_SIZES[0]: large}
    sizes |= {side: large.resize((side, side), Image.Resampling.LANCZOS) for side in AVATAR_SIZES[1:]}
    return {side: _webp(picture) for side, picture in sizes.items()}


def _open(data: bytes) -> Image.Image:
    """Abre solo el encabezado (sin decodificar) y rechaza lo que no es JPEG/PNG/WEBP o tiene demasiados píxeles."""
    if not data:
        raise _invalid("AVATAR_EMPTY")
    try:
        image = Image.open(io.BytesIO(data), formats=_FORMATS)
    except Image.DecompressionBombError as exc:
        raise _too_many_pixels() from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise _invalid("AVATAR_FORMAT") from exc
    if image.width * image.height > settings.AVATAR_MAX_MEGAPIXELS * 1_000_000:
        raise _too_many_pixels()
    return image


def _too_many_pixels() -> UnprocessableError:
    return _invalid("AVATAR_TOO_MANY_PIXELS", {"max": f"{settings.AVATAR_MAX_MEGAPIXELS:g}"})


def _oriented_size(image: Image.Image) -> tuple[int, int]:
    """Ancho y alto como se ve la foto (con su orientación EXIF aplicada). Un JPEG o WEBP no se decodifica para
    saberlo; un PNG sí (su EXIF puede venir después de los píxeles)."""
    orientation = image.getexif().get(ExifTags.Base.Orientation, 1)
    return (image.height, image.width) if orientation in _SWAPPED else image.size


def _crop_box(crop: CropBox | None, width: int, height: int) -> CropBox:
    """El recorte elegido (validado) o el cuadrado del centro."""
    minimum = settings.AVATAR_MIN_SIDE_PX
    if min(width, height) < minimum:
        raise _invalid("AVATAR_TOO_SMALL", {"min": minimum})
    if crop is None:
        side = min(width, height)
        return CropBox((width - side) // 2, (height - side) // 2, side)
    inside = crop.x + crop.size <= width and crop.y + crop.size <= height
    if crop.size < minimum or not inside:
        raise UnprocessableError(
            code="AVATAR_CROP_INVALID",
            key="AVATAR_CROP_OUTSIDE",
            params={"width": width, "height": height, "min": minimum},
            field="crop",
        )
    return crop


def _square(image: Image.Image, box: CropBox, width: int) -> Image.Image:
    """Decodifica (un JPEG ya reducido a lo necesario), aplica la orientación, recorta y deja los colores en sRGB
    con o sin transparencia."""
    if image.format in _JPEG:
        factor = box.size / AVATAR_SIZES[0]
        if factor > 1:  # decodificar a 1/2, 1/4 u 1/8 sin bajar de 512 px en el recorte
            image.draft(image.mode, (math.ceil(image.width / factor), math.ceil(image.height / factor)))
    image.load()
    oriented = ImageOps.exif_transpose(image)
    scale = oriented.width / width
    # En la escala decodificada; el redondeo nunca deja el cuadrado fuera de la imagen (sin bordes negros).
    side = min(round(box.size * scale), oriented.width, oriented.height)
    left = min(round(box.x * scale), oriented.width - side)
    top = min(round(box.y * scale), oriented.height - side)
    square = oriented.crop((left, top, left + side, top + side))
    return _plain(to_srgb(square))


def to_srgb(image: Image.Image) -> Image.Image:
    """Colores de un perfil RGB (Display P3, Adobe RGB...) llevados a sRGB, el de las pantallas y la web."""
    profile = image.info.get("icc_profile")
    if not profile or image.mode not in ("RGB", "RGBA"):
        return image
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
        return ImageCms.profileToProfile(image, source, _srgb_profile(), outputMode=image.mode) or image
    except OSError, ImageCms.PyCMSError:  # perfil dañado o de otro espacio de color: se usan tal cual
        return image


@cache
def _srgb_profile() -> ImageCms.ImageCmsProfile:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


def _plain(image: Image.Image) -> Image.Image:
    """RGB, o RGBA si la imagen tenía transparencia (paleta, gris, CMYK o 16 bits se convierten)."""
    alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    target = "RGBA" if alpha else "RGB"
    return image if image.mode == target else image.convert(target)


def _webp(image: Image.Image) -> bytes:
    """WebP SIN metadatos: se descarta todo lo que la imagen arrastra (EXIF, XMP, perfil de color, comentarios)
    y se codifica solo desde los píxeles."""
    image.info = {}
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=settings.AVATAR_WEBP_QUALITY, method=4)
    return buffer.getvalue()
