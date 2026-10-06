"""País y sistema autónomo (red) de una IP con la base LOCAL DB-IP Lite (decisión D8 del dueño del producto).

Por qué local: la IP de una persona nunca sale del servidor (regla 13 de la raíz). DB-IP publica cada mes dos archivos
MMDB gratuitos con licencia CC BY 4.0 (atribución en el README, "Licencias"): país (`dbip-country-lite`) y sistema
autónomo (`dbip-asn-lite`). Los descarga y verifica `app/services/ip_database.py`; este módulo solo los LEE.

- Lectura en memoria con `maxminddb` (extensión en C con el archivo mapeado: los procesos de la réplica comparten las
  páginas del sistema operativo), del orden de microsegundos por consulta y sin tocar la base de datos.
- Cada proceso revisa cada `IP_DB_RELOAD_CHECK_SECONDS` si el archivo cambió (otro proceso o réplica lo actualizó con
  un reemplazo atómico) y lo vuelve a abrir: nada que invalidar a mano y correcto con N réplicas.
- Tolerante a fallas (regla 7): sin archivo, con uno dañado o con una IP privada, la consulta responde `None` y las
  señales de red simplemente no se miden. Un archivo que no abre se registra UNA vez por cambio del archivo (no en
  cada consulta).
"""

import ipaddress
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import maxminddb

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Huella de un archivo en disco: si cambia, se vuelve a abrir (reemplazo atómico de otro proceso).
type FileStamp = tuple[int, int, int] | None


@dataclass(frozen=True)
class IpInfo:
    """Lo que la base local sabe de una IP pública."""

    #: País (ISO 3166-1 alfa-2, mayúsculas) o None si la base de país no está.
    country: str | None
    #: Sistema autónomo y su organización, o None si la base de ASN no está.
    asn: int | None
    organization: str | None
    #: La red es de una nube o un centro de datos (`IP_HOSTING_ASNS` / `IP_HOSTING_KEYWORDS`).
    hosting: bool


def _stamp(path: Path) -> FileStamp:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size, info.st_ino


class MmdbFile:
    """Un archivo MMDB abierto, que se vuelve a abrir cuando cambia en disco."""

    def __init__(self, path_of: Any) -> None:
        #: Función que da la ruta vigente (las pruebas cambian la configuración).
        self._path_of = path_of
        self._lock = threading.Lock()
        self._reader: Any = None
        self._stamp: FileStamp = None
        self._checked = 0.0

    def get(self, ip: str) -> dict[str, Any] | None:
        reader = self._current()
        if reader is None:
            return None
        try:
            record = reader.get(ip)
        except ValueError:  # IPv6 en una base solo IPv4 (o una dirección que la base no acepta)
            return None
        return record if isinstance(record, dict) else None

    def built(self) -> tuple[str, datetime] | None:
        """El tipo del archivo abierto y cuándo lo construyó DB-IP (lo ve el ADMIN en Seguridad facial)."""
        reader = self._current()
        if reader is None:
            return None
        metadata = reader.metadata()
        return str(metadata.database_type), datetime.fromtimestamp(metadata.build_epoch, UTC)

    def reload(self) -> None:
        """Obliga a revisar el archivo en la siguiente consulta."""
        with self._lock:
            self._checked = 0.0

    def close(self) -> None:
        with self._lock:
            self._swap(None, None)
            self._checked = 0.0

    def _current(self) -> Any:
        now = time.monotonic()
        with self._lock:
            if self._checked and now - self._checked < settings.IP_DB_RELOAD_CHECK_SECONDS:
                return self._reader
            self._checked = now
            path = Path(self._path_of())
            stamp = _stamp(path)
            if stamp != self._stamp:
                self._swap(_open(path) if stamp is not None else None, stamp)
            return self._reader

    def _swap(self, reader: Any, stamp: FileStamp) -> None:
        old, self._reader, self._stamp = self._reader, reader, stamp
        if old is not None:
            old.close()


def _open(path: Path) -> Any:
    """Abre el archivo (mapeado en memoria); uno dañado se registra y queda sin base hasta que cambie."""
    try:
        return maxminddb.open_database(str(path), mode=maxminddb.MODE_AUTO)
    except OSError, ValueError, maxminddb.InvalidDatabaseError:
        logger.exception("No se pudo abrir la base local de IP %s: las señales de red no se miden", path.name)
        return None


def is_hosting(asn: int | None, organization: str | None) -> bool:
    """La red es de una nube o de un centro de datos (VPN, servidor intermediario o un programa que llama a la API)."""
    if asn is not None and asn in settings.IP_HOSTING_ASNS:
        return True
    name = (organization or "").lower()
    return bool(name) and any(word in name for word in settings.IP_HOSTING_KEYWORDS)


class IpIntel:
    """País y red de una IP pública (las dos bases son independientes: una puede faltar)."""

    def __init__(self) -> None:
        self.country_db = MmdbFile(lambda: settings.ip_country_db)
        self.asn_db = MmdbFile(lambda: settings.ip_asn_db)

    def lookup(self, ip: str | None) -> IpInfo | None:
        """None si no hay base, si la IP no es válida o si no es pública (red local, del gateway o de pruebas)."""
        if not ip:
            return None
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if not address.is_global:
            return None
        text = str(address)
        place, network = self.country_db.get(text), self.asn_db.get(text)
        if place is None and network is None:
            return None
        country = ((place or {}).get("country") or {}).get("iso_code")
        asn = (network or {}).get("autonomous_system_number")
        organization = (network or {}).get("autonomous_system_organization")
        return IpInfo(
            country=str(country).upper() if country else None,
            asn=int(asn) if isinstance(asn, int) else None,
            organization=str(organization)[:120] if organization else None,
            hosting=is_hosting(asn if isinstance(asn, int) else None, organization),
        )

    def reload(self) -> None:
        self.country_db.reload()
        self.asn_db.reload()

    def close(self) -> None:
        self.country_db.close()
        self.asn_db.close()


#: La base de este proceso (se abre al primer uso).
ip_intel = IpIntel()
