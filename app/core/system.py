"""Recursos del sistema disponibles para el proceso."""

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
