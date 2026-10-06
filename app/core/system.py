"""Recursos del sistema disponibles para el proceso."""

import ipaddress
import os


def available_cpus() -> int:
    """Núcleos realmente disponibles: afinidad del proceso y cuota de cgroups (Docker --cpus)."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError, OSError:
        cpus = os.cpu_count() or 1
    try:
        with open("/sys/fs/cgroup/cpu.max") as fh:  # cgroups v2
            quota, period = fh.read().split()
        if quota != "max":
            cpus = min(cpus, max(1, int(int(quota) / int(period))))
    except OSError, ValueError:
        pass
    return max(1, cpus)


def default_api_workers() -> int:
    """Procesos de la API por defecto: la mitad de los núcleos (la otra mitad queda para el
    reconocimiento facial, que reparte sus workers entre procesos), entre 1 y 4."""
    return max(1, min(4, available_cpus() // 2))


#: Rutas IPv4 del sistema (Linux): de aquí salen las redes a las que está conectado el contenedor.
ROUTES_FILE = "/proc/net/route"


def local_networks(routes_file: str = ROUTES_FILE) -> list[str]:
    """Redes IPv4 conectadas directamente a este contenedor (CIDR), sin la ruta por defecto ni `lo`.

    En docker compose es la red del proyecto: ahí viven el gateway Nginx y las demás réplicas. El archivo
    trae destino y máscara en hexadecimal con el orden de bytes del equipo (little-endian en x86 y ARM)."""
    try:
        with open(routes_file) as fh:
            rows = [line.split() for line in fh.read().splitlines()[1:]]
    except OSError:
        return []
    networks: list[str] = []
    for row in rows:
        if len(row) < 8 or row[0] == "lo" or int(row[7], 16) == 0:
            continue
        destination = ipaddress.IPv4Address(int(row[1], 16).to_bytes(4, "little"))
        prefix = bin(int(row[7], 16)).count("1")
        network = str(ipaddress.IPv4Network(f"{destination}/{prefix}", strict=False))
        if network not in networks:
            networks.append(network)
    return networks


def trusted_proxies(routes_file: str = ROUTES_FILE) -> str:
    """`--forwarded-allow-ips` de uvicorn con FORWARDED_ALLOW_IPS=auto: este mismo equipo y sus redes
    locales. Solo el proxy (que está en esa red) puede decir cuál es la IP real del cliente; la API no publica
    puertos, así que desde fuera nadie la alcanza para inventarla."""
    return ",".join(["127.0.0.1", *local_networks(routes_file)])
