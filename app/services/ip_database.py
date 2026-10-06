"""Actualización mensual de la base local de IP (DB-IP Lite, decisión D8 del dueño del producto).

DB-IP publica cada mes los archivos gratuitos (CC BY 4.0) de país y de sistema autónomo; `app/core/ip_intel.py` los
lee. Este módulo los mantiene al día FUERA de las peticiones (el mantenimiento de cada proceso lo llama en cada vuelta y
`python -m app.cli ipdb refresh` lo hace bajo demanda):

1. Solo si toca: el archivo falta o tiene más de `IP_DB_REFRESH_DAYS` días; tras una falla espera `IP_DB_RETRY_HOURS`
   (sin red o sin acceso, no se reintenta en cada vuelta ni se llena la bandeja de errores).
2. Un proceso a la vez por carpeta: candado de archivo (`flock`) sin esperar. Las réplicas que comparten la carpeta
   (volumen de docker compose) descargan una vez; las demás ven el archivo nuevo y lo vuelven a abrir solas.
3. Descarga con tiempo límite (`IP_DB_DOWNLOAD_TIMEOUT_SECONDS`) y tope de tamaño (`IP_DB_MAX_MB`) del mes en curso (o
   del anterior si aún no se publica), lo descomprime (el CRC del gzip lo comprueba) y exige que su SHA-1 sea uno de
   los que publica la página de DB-IP (`IP_DB_*_CHECKSUM_URL`).
4. Comprueba que el archivo abre y es del tipo esperado, y lo reemplaza de forma ATÓMICA (`os.replace` en la misma
   carpeta): ningún lector ve un archivo a medias.

Si algo falla, el archivo anterior sigue (o, si no había, las señales de red no se miden) y la falla se registra
(regla 4: un error en segundo plano llega a "Errores del sistema").
"""

import fcntl
import gzip
import hashlib
import io
import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import maxminddb

from app.core.config import settings
from app.core.ip_intel import ip_intel
from app.core.observability import observed

logger = logging.getLogger(__name__)

#: Resultado de cada archivo en una vuelta.
UPDATED = "updated"
FRESH = "fresh"
FAILED = "failed"
BUSY = "busy"
WAITING = "waiting"
#: Un SHA-1 en la página de descargas (40 dígitos hexadecimales).
_SHA1 = re.compile(r"\b[0-9a-f]{40}\b")
_MB = 1024 * 1024


class IpDatabaseError(Exception):
    """El archivo descargado no sirve (no publicado, muy grande, otra suma o de otro tipo)."""


@dataclass(frozen=True)
class Source:
    """Un archivo de la base: dónde vive, de dónde se descarga y qué tipo debe decir su metadato."""

    kind: str
    path: Path
    url: str
    checksum_url: str
    #: Texto que contiene el `database_type` del archivo (DB-IP: "DBIP-Country-Lite", "DBIP-ASN-Lite").
    database_type: str


def sources() -> tuple[Source, ...]:
    country = Source(
        "country", settings.ip_country_db, settings.IP_DB_COUNTRY_URL, settings.IP_DB_COUNTRY_CHECKSUM_URL, "country"
    )
    return country, Source("asn", settings.ip_asn_db, settings.IP_DB_ASN_URL, settings.IP_DB_ASN_CHECKSUM_URL, "asn")


#: Cuándo falló por última vez cada archivo en ESTE proceso (solo decide cuándo reintentar).
_failures: dict[str, float] = {}


def fetch(url: str) -> bytes:
    """Descarga con tiempo límite y tope de tamaño (lo que pase del tope no se lee)."""
    limit = settings.IP_DB_MAX_MB * _MB
    request = Request(url, headers={"User-Agent": "employee-time-clock/ipdb"})  # noqa: S310 - URL de la configuración
    with urlopen(request, timeout=settings.IP_DB_DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310
        data: bytes = response.read(limit + 1)
    if len(data) > limit:
        raise IpDatabaseError(f"La descarga pasa de {settings.IP_DB_MAX_MB} MB: {url}")
    return data


@observed("ipdb.refresh")
def refresh_if_due(*, force: bool = False, now: datetime | None = None) -> dict[str, str]:
    """Actualiza los archivos que lo necesitan; devuelve el resultado de cada uno (vacío si está apagado)."""
    if not settings.IP_DB_REFRESH_ENABLED:
        return {}
    moment = now or datetime.now(UTC)
    return {source.kind: _refresh(source, moment, force) for source in sources() if force or _due(source.path, moment)}


def _due(path: Path, now: datetime) -> bool:
    try:
        written = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return True
    return now - written >= timedelta(days=settings.IP_DB_REFRESH_DAYS)


def _refresh(source: Source, now: datetime, force: bool) -> str:
    failed = _failures.get(source.kind)
    if not force and failed is not None and time.monotonic() - failed < settings.IP_DB_RETRY_HOURS * 3600:
        return WAITING
    try:
        source.path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive(source.path) as locked:
            if not locked:
                return BUSY
            if not force and not _due(source.path, now):
                return FRESH  # otro proceso lo acaba de actualizar
            _download(source, now)
    except Exception:
        _failures[source.kind] = time.monotonic()
        hours = settings.IP_DB_RETRY_HOURS
        logger.exception("No se pudo actualizar la base local de IP (%s); se reintenta en %s h", source.kind, hours)
        return FAILED
    _failures.pop(source.kind, None)
    ip_intel.reload()
    logger.info("Base local de IP actualizada: %s", source.path.name)
    return UPDATED


@contextmanager
def _exclusive(path: Path) -> Iterator[bool]:
    """Candado de archivo (sin esperar) junto al archivo: un solo proceso descarga a la vez."""
    with open(path.with_name(f".{path.name}.lock"), "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _months(now: datetime) -> tuple[date, date]:
    """El mes en curso y el anterior (el 1.º del mes el archivo nuevo puede no estar publicado todavía)."""
    first = now.date().replace(day=1)
    return first, (first - timedelta(days=1)).replace(day=1)


def _download(source: Source, now: datetime) -> None:
    packed = None
    for month in _months(now):
        try:
            packed = fetch(source.url.format(year=f"{month.year:04d}", month=f"{month.month:02d}"))
            break
        except HTTPError as exc:
            if exc.code != 404:
                raise
    if packed is None:
        raise IpDatabaseError(f"DB-IP no publica el archivo de {source.kind} de este mes ni del anterior")
    data = _gunzip(packed)
    digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()
    if digest not in set(_SHA1.findall(fetch(source.checksum_url).decode("utf-8", errors="ignore"))):
        raise IpDatabaseError(f"La suma SHA-1 del archivo de {source.kind} no coincide con la publicada")
    temp = source.path.with_name(f".{source.path.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _validate(temp, source)
        os.replace(temp, source.path)
    finally:
        temp.unlink(missing_ok=True)


def _gunzip(packed: bytes) -> bytes:
    limit = settings.IP_DB_MAX_MB * _MB
    with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise IpDatabaseError(f"El archivo descomprimido pasa de {settings.IP_DB_MAX_MB} MB")
    return data


def _validate(path: Path, source: Source) -> None:
    """Abre el archivo nuevo y revisa su tipo antes de reemplazar el vigente."""
    with maxminddb.open_database(str(path), mode=maxminddb.MODE_MEMORY) as reader:
        kind = str(reader.metadata().database_type).lower()
    if source.database_type not in kind:
        raise IpDatabaseError(f"El archivo de {source.kind} es de otro tipo: {kind}")
