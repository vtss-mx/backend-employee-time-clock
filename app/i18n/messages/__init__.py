"""Catálogo de mensajes de la API en sus dos idiomas (regla 16 de la raíz).

Cada texto que una persona lee (el `message` del sobre, el de cada error, los textos dentro de `data`) vive aquí con
una llave estable, en `es_mx/` (español de México, el de omisión) y en `en_us/` (inglés natural de Estados Unidos,
con los mismos términos que la aplicación web). Los catálogos de la BD (nombres de estados, motivos, pantallas...)
viven en la BD con su traducción en `catalog.translations`: no se repiten aquí.
"""

from collections.abc import Mapping
from typing import Final

from app.i18n.locale import Locale
from app.i18n.messages import en_us, es_mx
from app.i18n.messages.base import Message

MESSAGES: Final[Mapping[Locale, Mapping[str, Message]]] = {"es-MX": es_mx.MESSAGES, "en-US": en_us.MESSAGES}

__all__ = ["MESSAGES", "Message"]
