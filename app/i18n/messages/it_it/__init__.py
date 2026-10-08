"""Catálogo de mensajes en italiano (it-IT): las áreas unidas en uno solo (una llave repetida es un error)."""

from app.i18n.messages.base import merge
from app.i18n.messages.it_it import account, attendance, billing, core, documents, face, people, platform

#: Módulos por área (los mismos en cada idioma: `tests/test_i18n.py` los compara uno a uno).
AREAS = (core, account, people, face, attendance, platform, billing, documents)
MESSAGES = merge(*(area.MESSAGES for area in AREAS))
