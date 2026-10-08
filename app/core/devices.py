"""Clasificación del dispositivo del cliente: teléfono, tableta o computadora.

Usa la cabecera User-Agent y, cuando el navegador la envía, la Client Hint `Sec-CH-UA-Mobile`
(Chrome, Edge y Samsung Internet: "?1" en teléfonos, "?0" en tabletas y computadoras).

Es una política de uso, no un control de seguridad: un cliente puede falsear ambas cabeceras.
"""

import re
from typing import Literal

DeviceKind = Literal["phone", "tablet", "desktop"]
#: Plataforma gruesa de un intento facial (deriva de señales, antifraude fase 3): en iOS todo navegador es WebKit
#: (iPhone y iPad: "IOS_SAFARI"); en Android casi todos son Chromium (Chrome, Edge, Samsung Internet:
#: "ANDROID_CHROME") salvo Firefox; una computadora es "DESKTOP"; lo que no se reconoce (o sin User-Agent), "OTHER".
#: Nunca la persona.
Platform = Literal["IOS_SAFARI", "ANDROID_CHROME", "DESKTOP", "OTHER"]
PLATFORMS: tuple[Platform, ...] = ("IOS_SAFARI", "ANDROID_CHROME", "DESKTOP", "OTHER")

# iPadOS moderno se presenta como Mac de escritorio ("Macintosh"): queda como computadora, lo
# cual también la excluye. Los iPad antiguos y Firefox en tabletas Android sí se identifican.
_TABLET = re.compile(r"iPad|Tablet|PlayBook|Kindle|Silk/", re.IGNORECASE)
# "Mobi" es la marca recomendada (MDN) para teléfonos; Android sin "Mobile" es una tableta.
_PHONE = re.compile(r"iPhone|iPod|Mobi|Windows Phone|BlackBerry|BB10|Opera Mini", re.IGNORECASE)
_ANDROID = re.compile(r"Android", re.IGNORECASE)
_IOS = re.compile(r"iPhone|iPad|iPod", re.IGNORECASE)
_DESKTOP_OS = re.compile(r"Windows|Macintosh|Mac OS X|CrOS|Linux", re.IGNORECASE)
_FIREFOX = re.compile(r"Firefox/", re.IGNORECASE)


def platform_of(user_agent: str | None) -> Platform:
    """La plataforma gruesa del navegador (`Platform`), para agrupar las señales del motor sin guardar nada de la
    persona. iPadOS moderno se presenta como Mac: cae en DESKTOP (lo mismo que `classify_device`)."""
    ua = user_agent or ""
    if _IOS.search(ua):
        return "IOS_SAFARI"
    if _ANDROID.search(ua):
        return "OTHER" if _FIREFOX.search(ua) else "ANDROID_CHROME"
    if _DESKTOP_OS.search(ua):
        return "DESKTOP"
    return "OTHER"


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


#: Sistema operativo y navegador que se reconocen en el User-Agent, en orden (el primero que coincide gana). Son
#: nombres propios: se muestran igual en cualquier idioma.
_SYSTEMS = (
    ("iPhone", re.compile(r"iPhone|iPod", re.IGNORECASE)),
    ("iPad", re.compile(r"iPad", re.IGNORECASE)),
    ("Android", re.compile(r"Android", re.IGNORECASE)),
    ("ChromeOS", re.compile(r"CrOS", re.IGNORECASE)),
    ("Windows", re.compile(r"Windows", re.IGNORECASE)),
    ("Mac", re.compile(r"Macintosh|Mac OS X", re.IGNORECASE)),
    ("Linux", re.compile(r"Linux", re.IGNORECASE)),
)
_BROWSERS = (
    ("Edge", re.compile(r"Edg(A|iOS)?/", re.IGNORECASE)),
    ("Opera", re.compile(r"OPR/|Opera", re.IGNORECASE)),
    ("Samsung Internet", re.compile(r"SamsungBrowser", re.IGNORECASE)),
    ("Firefox", re.compile(r"Firefox/|FxiOS", re.IGNORECASE)),
    ("Chrome", re.compile(r"Chrome/|CriOS", re.IGNORECASE)),
    ("Safari", re.compile(r"Safari/", re.IGNORECASE)),
)


def device_label(user_agent: str | None) -> str | None:
    """Nombre amigable del dispositivo por su navegador ("iPhone · Safari", "Android · Chrome"); None si el
    User-Agent no dice nada reconocible (quien lo muestra usa entonces el tipo: teléfono, tableta o computadora)."""
    ua = user_agent or ""
    system = next((name for name, pattern in _SYSTEMS if pattern.search(ua)), None)
    browser = next((name for name, pattern in _BROWSERS if pattern.search(ua)), None)
    parts = [part for part in (system, browser) if part]
    return " · ".join(parts) or None
