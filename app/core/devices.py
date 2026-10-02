"""Clasificación del dispositivo del cliente: teléfono, tableta o computadora.

Usa la cabecera User-Agent y, cuando el navegador la envía, la Client Hint `Sec-CH-UA-Mobile`
(Chrome, Edge y Samsung Internet: "?1" en teléfonos, "?0" en tabletas y computadoras).

Es una política de uso, no un control de seguridad: un cliente puede falsear ambas cabeceras.
"""

import re
from typing import Literal

DeviceKind = Literal["phone", "tablet", "desktop"]

# iPadOS moderno se presenta como Mac de escritorio ("Macintosh"): queda como computadora, lo
# cual también la excluye. Los iPad antiguos y Firefox en tabletas Android sí se identifican.
_TABLET = re.compile(r"iPad|Tablet|PlayBook|Kindle|Silk/", re.IGNORECASE)
# "Mobi" es la marca recomendada (MDN) para teléfonos; Android sin "Mobile" es una tableta.
_PHONE = re.compile(r"iPhone|iPod|Mobi|Windows Phone|BlackBerry|BB10|Opera Mini", re.IGNORECASE)
_ANDROID = re.compile(r"Android", re.IGNORECASE)


def classify_device(user_agent: str | None, ch_mobile: str | None = None) -> DeviceKind:
    ua = user_agent or ""
    if _TABLET.search(ua):
        return "tablet"
    hint = (ch_mobile or "").strip()
    if hint in ("?1", "?0"):
        if hint == "?1":
            return "phone"
        return "tablet" if _ANDROID.search(ua) else "desktop"
    if _PHONE.search(ua):
        return "phone"
    return "tablet" if _ANDROID.search(ua) else "desktop"
