"""Catálogo de mensajes de la API en sus siete idiomas (regla 16 de la raíz).

Cada texto que una persona lee (el `message` del sobre, el de cada error, los textos dentro de `data`) vive aquí con
una llave estable, en `es_mx/` (español de México, el de omisión), `en_us/`, `pt_br/`, `fr_fr/`, `de_de/`, `it_it/` y
`es_es/` (derivado de `es_mx`: solo el vocabulario de España), con el registro y los términos de
`docs/i18n/glosario.md` y los mismos que la aplicación web. Los catálogos de la BD (nombres de estados, motivos,
pantallas...) viven en la BD con su traducción en `catalog.translations`: no se repiten aquí.
"""

from collections.abc import Mapping
from typing import Final

from app.i18n.locale import Locale
from app.i18n.messages import de_de, en_us, es_es, es_mx, fr_fr, it_it, pt_br
from app.i18n.messages.base import Message

MESSAGES: Final[Mapping[Locale, Mapping[str, Message]]] = {
    "es-MX": es_mx.MESSAGES,
    "en-US": en_us.MESSAGES,
    "pt-BR": pt_br.MESSAGES,
    "fr-FR": fr_fr.MESSAGES,
    "de-DE": de_de.MESSAGES,
    "it-IT": it_it.MESSAGES,
    "es-ES": es_es.MESSAGES,
}

__all__ = ["MESSAGES", "Message"]
