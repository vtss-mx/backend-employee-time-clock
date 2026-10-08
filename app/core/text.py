"""Texto comparable: sin acentos ni mayúsculas, en UN solo lugar.

Lo usan las reglas que comparan lo que escribe una persona o un sistema con una lista propia: el nombre de una
cámara virtual («Câmera virtual», «Caméra virtuelle», «Virtuelle Kamera»: el sistema operativo lo escribe en SU
idioma, con o sin acentos; decisión D-C4 de `docs/rd/compatibilidad-biometria.md`) y el orden alfabético de los
catálogos («Åland» junto a «Albania»). La aplicación web pliega igual (`foldText` de `utils/text.ts`): las dos
puntas llegan a la misma conclusión sobre la misma cámara.
"""

import unicodedata


def fold_text(text: str) -> str:
    """El texto sin acentos ni mayúsculas («Câmera Virtual» → «camera virtual»)."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).casefold()
