"""Huella perceptual de una imagen (pHash DCT de 64 bits), sin librerías nuevas (OpenCV y NumPy ya instalados).

El SHA-256 de los píxeles (`capture_digest`) solo reconoce la MISMA imagen: volver a comprimir el JPEG o mover un
nivel de brillo lo cambia por completo (prototipo P2 de docs/rd/antifraude-identidad.md: 0 % de reenvíos
modificados detectados). El pHash describe la FORMA de la imagen en sus frecuencias bajas: se reduce a 32×32 en
gris, se toma la transformada de coseno (DCT) y los 8×8 coeficientes de frecuencia más baja; cada bit dice si un
coeficiente está sobre la mediana. Dos versiones de la misma captura quedan a pocos bits (P2: ≤ 8 en el 96-100 % de
los reenvíos modificados); dos fotos distintas de la misma persona, a 10 o más.

Se guarda como entero de 64 bits CON signo (BIGINT de PostgreSQL) y se compara por distancia de Hamming.
"""

import cv2
import numpy as np

#: Lado de la imagen reducida y de la esquina de frecuencias bajas que forma la huella (8×8 = 64 bits).
_SIDE = 32
_LOW = 8
_MASK = (1 << 64) - 1


def phash(gray: np.ndarray) -> int:
    """pHash de 64 bits de una imagen en gris (cualquier tamaño), como entero con signo."""
    small = cv2.resize(gray.astype(np.float32), (_SIDE, _SIDE), interpolation=cv2.INTER_AREA)
    low = cv2.dct(small)[:_LOW, :_LOW].flatten()
    # La mediana sin el término de continua (el brillo medio: no describe la forma).
    bits = low > float(np.median(low[1:]))
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return signed(value)


def signed(value: int) -> int:
    """El entero de 64 bits sin signo como BIGINT (con signo)."""
    value &= _MASK
    return value - (1 << 64) if value >= 1 << 63 else value


def distance(a: int, b: int) -> int:
    """Bits distintos entre dos huellas (distancia de Hamming)."""
    return ((a ^ b) & _MASK).bit_count()


def to_hex(value: int) -> str:
    """La huella en 16 dígitos hexadecimales (como viaja en la lista de bloqueo)."""
    return f"{value & _MASK:016x}"


def from_hex(text: str) -> int:
    return signed(int(text, 16))
