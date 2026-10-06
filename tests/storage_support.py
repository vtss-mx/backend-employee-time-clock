"""Bucket falso (en memoria) para probar el almacenamiento de imágenes sin red ni credenciales.

Implementa la misma interfaz que `GcsStorage` (`app/core/object_storage.ObjectStorage`), incluida la
subida "solo si no existe", y permite provocar cada falla: una operación caída (`down`), un bucket que
reporta otra cosa de lo que se subió (`tamper`) o un objeto que no se puede descifrar (`swap_object`).
Todas las pruebas lo usan por omisión (`conftest._db`): ninguna toca el bucket real.
"""

import hashlib
from collections.abc import Mapping

from app.core.object_storage import (
    ObjectExists,
    ObjectInfo,
    ObjectNotFound,
    StorageUnavailable,
    content_md5,
)


class FakeStorage:
    configured = True

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        #: Operaciones que fallan como si el bucket no respondiera: put, stat, get, delete, list.
        self.down: set[str] = set()
        #: El bucket reporta un MD5 distinto del subido (subida dañada).
        self.tamper = False
        self.calls: list[tuple[str, str]] = []

    def describe(self) -> dict[str, str | None]:
        return {"backend": "gcs", "bucket": "bucket-de-pruebas", "reason": None}

    def _check(self, operation: str, name: str) -> None:
        self.calls.append((operation, name))
        if operation in self.down:
            raise StorageUnavailable(f"{operation} no respondió (simulado)")

    def _info(self, name: str) -> ObjectInfo:
        data, metadata = self.objects[name]
        md5 = content_md5(data + b"!" if self.tamper else data)
        return ObjectInfo(name=name, size=len(data), md5=md5, metadata=dict(metadata))

    def put(self, name: str, data: bytes, metadata: Mapping[str, str], *, interactive: bool = False) -> ObjectInfo:
        self._check("put", name)
        if name in self.objects:
            raise ObjectExists(f"ya existe un objeto llamado {name}")
        self.objects[name] = (bytes(data), dict(metadata))
        return self._info(name)

    def stat(self, name: str, *, interactive: bool = False) -> ObjectInfo | None:
        self._check("stat", name)
        return self._info(name) if name in self.objects else None

    def get(self, name: str, *, interactive: bool = False) -> bytes:
        self._check("get", name)
        if name not in self.objects:
            raise ObjectNotFound("el objeto no existe en el bucket")
        return self.objects[name][0]

    def delete(self, name: str, *, interactive: bool = False) -> None:
        self._check("delete", name)
        self.objects.pop(name, None)

    def list_names(self, prefix: str, *, limit: int) -> list[str]:
        self._check("list", prefix)
        return sorted(name for name in self.objects if name.startswith(prefix))[:limit]


#: Contenido cifrado con otra llave: pasa la verificación del bucket y falla al descifrarse.
UNREADABLE = b"cifrado-con-otra-llave"


def swap_object(storage: FakeStorage, name: str, payload: bytes = UNREADABLE) -> str:
    """Cambia el objeto `name` por `payload` (ya cifrado o, por omisión, ilegible) y devuelve su SHA-256
    para anotarlo en la fila: así la lectura pasa la verificación y llega a descifrarlo."""
    storage.objects[name] = (payload, {})
    return hashlib.sha256(payload).hexdigest()
