"""Forma del catálogo de mensajes: llave estable → texto, o sus formas de plural.

Un idioma es un paquete (`es_mx`, `en_us`) con los mismos módulos por área (`core`, `people`, `attendance`...); cada
módulo define `MESSAGES` y el paquete los une con `merge`, que no permite una llave repetida (dos áreas que dijeran
cosas distintas con la misma llave). `tests/test_i18n.py` exige que los dos idiomas tengan exactamente las mismas
llaves por módulo, los mismos `{parámetros}` y las mismas formas de plural.
"""

from collections.abc import Mapping

#: Un mensaje: su texto, o sus formas de plural (`one`, `other` y, si el cero lleva otro texto, `zero`) que se
#: eligen con el parámetro `count`.
type Message = str | Mapping[str, str]
type Messages = Mapping[str, Message]


def merge(*areas: Messages) -> dict[str, Message]:
    """Las áreas de un idioma en un solo catálogo; una llave en dos áreas es un error de quien la agregó."""
    merged: dict[str, Message] = {}
    for area in areas:
        repeated = merged.keys() & area.keys()
        if repeated:
            raise ValueError(f"Llaves de mensajes repetidas entre áreas: {sorted(repeated)}")
        merged.update(area)
    return merged
