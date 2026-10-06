"""Bases MMDB diminutas para las pruebas (formato MaxMind DB 2.0, solo IPv4), generadas al vuelo: las pruebas nunca
descargan la base de DB-IP ni guardan un archivo binario en el repositorio.

`write_mmdb(ruta, tipo, {"203.0.113.0/24": {...}})` escribe el árbol de búsqueda (registros de 24 bits), el separador,
la sección de datos (mapas, textos y enteros) y los metadatos que `maxminddb` exige. `install(...)` deja las dos bases
(país y sistema autónomo) donde las busca la configuración de las pruebas y obliga a volver a abrirlas.
"""

import gzip
import ipaddress
from pathlib import Path

from app.core.config import settings
from app.core.ip_intel import ip_intel

#: Redes de prueba (públicas para `ipaddress`: las de documentación no lo son y la base las ignoraría).
MX_HOME = "187.188.1.10"  # casa en México (sistema autónomo de un proveedor de internet)
MX_MOBILE = "189.200.1.10"  # datos móviles en México (otro sistema autónomo)
US_CLOUD = "52.95.1.10"  # una nube en Estados Unidos (centro de datos)
US_HOME = "73.10.1.10"  # casa en Estados Unidos
COUNTRIES = {
    "187.188.0.0/16": {"country": {"iso_code": "MX"}},
    "189.200.0.0/16": {"country": {"iso_code": "MX"}},
    "52.95.0.0/16": {"country": {"iso_code": "US"}},
    "73.10.0.0/16": {"country": {"iso_code": "US"}},
}
NETWORKS = {
    "187.188.0.0/16": {"autonomous_system_number": 22884, "autonomous_system_organization": "Proveedor de Casa"},
    "189.200.0.0/16": {"autonomous_system_number": 28403, "autonomous_system_organization": "Red Movil"},
    "52.95.0.0/16": {"autonomous_system_number": 16509, "autonomous_system_organization": "Amazon.com, Inc."},
    "73.10.0.0/16": {"autonomous_system_number": 7922, "autonomous_system_organization": "Comcast"},
}
_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"


def _control(kind: int, size: int) -> bytes:
    first, extended = (kind << 5, b"") if kind <= 7 else (0, bytes([kind - 7]))
    if size < 29:
        return bytes([first | size]) + extended
    return bytes([first | 29]) + extended + bytes([size - 29])


def _uint(kind: int, value: int) -> bytes:
    payload = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    return _control(kind, len(payload)) + payload


def encode(value: object) -> bytes:
    """Un dato en el formato de la sección de datos (los tipos que usan las bases de país y de red)."""
    if isinstance(value, str):
        raw = value.encode()
        return _control(2, len(raw)) + raw
    if isinstance(value, int):
        return _uint(6 if value < 2**32 else 9, value)
    if isinstance(value, dict):
        return _control(7, len(value)) + b"".join(encode(key) + encode(item) for key, item in value.items())
    assert isinstance(value, list)
    return _control(11, len(value)) + b"".join(encode(item) for item in value)


def build(database_type: str, networks: dict[str, dict]) -> bytes:
    """El archivo MMDB (IPv4) con cada red apuntando a su registro."""
    nodes: list[list[object]] = [[None, None]]
    data, offsets = b"", []
    for cidr, record in networks.items():
        offsets.append(len(data))
        data += encode(record)
        network = ipaddress.ip_network(cidr)
        bits = format(int(network.network_address), "032b")[: network.prefixlen]
        node = 0
        for depth, bit in enumerate(bits):
            side = int(bit)
            if depth == len(bits) - 1:
                nodes[node][side] = ("data", offsets[-1])
                break
            if not isinstance(nodes[node][side], int):
                nodes.append([None, None])
                nodes[node][side] = len(nodes) - 1
            node = nodes[node][side]  # type: ignore[assignment]
    count = len(nodes)

    def record_value(record: object) -> int:
        if record is None:
            return count
        if isinstance(record, int):
            return record
        return count + 16 + record[1]  # type: ignore[index]

    tree = b"".join(
        record_value(left).to_bytes(3, "big") + record_value(right).to_bytes(3, "big") for left, right in nodes
    )
    metadata = {
        "binary_format_major_version": 2,
        "binary_format_minor_version": 0,
        "build_epoch": 1_790_000_000,
        "database_type": database_type,
        "description": {"en": "Base de pruebas"},
        "ip_version": 4,
        "languages": ["en"],
        "node_count": count,
        "record_size": 24,
    }
    meta = _control(7, len(metadata)) + b"".join(encode(key) + _meta(key, item) for key, item in metadata.items())
    return tree + bytes(16) + data + _METADATA_MARKER + meta


#: Metadatos que libmaxminddb exige de 16 bits y el de 64 bits.
_UINT16 = frozenset({"binary_format_major_version", "binary_format_minor_version", "ip_version", "record_size"})


def _meta(key: str, value: object) -> bytes:
    if key in _UINT16:
        return _uint(5, value)  # type: ignore[arg-type]
    if key == "build_epoch":
        return _uint(9, value)  # type: ignore[arg-type]
    return encode(value)


def write_mmdb(path: Path, database_type: str, networks: dict[str, dict]) -> bytes:
    data = build(database_type, networks)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def gzipped(data: bytes) -> bytes:
    return gzip.compress(data)


def install(*, country: bool = True, asn: bool = True) -> None:
    """Las dos bases de prueba donde las busca la configuración (o sin la que se pida) y las vuelve a abrir."""
    for wanted, path, kind, networks in (
        (country, settings.ip_country_db, "DBIP-Country-Lite", COUNTRIES),
        (asn, settings.ip_asn_db, "DBIP-ASN-Lite (compat=GeoLite2-ASN)", NETWORKS),
    ):
        if wanted:
            write_mmdb(path, kind, networks)
        else:
            path.unlink(missing_ok=True)
    ip_intel.reload()


def uninstall() -> None:
    for path in (settings.ip_country_db, settings.ip_asn_db):
        path.unlink(missing_ok=True)
    ip_intel.close()
