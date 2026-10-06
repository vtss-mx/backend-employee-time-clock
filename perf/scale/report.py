"""Resumen de perf/scale/run.sh (perf/results/scale/report.md): carga con 1 y N réplicas, reparto por réplica,
conexiones a PostgreSQL contra su presupuesto, canal WebSocket, mantenimiento y caída de réplicas."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def metric(summary: dict, name: str, key: str) -> float:
    return float(summary.get("metrics", {}).get(name, {}).get(key, 0.0))


def replicas(path: Path) -> dict[str, str]:
    """IP → nombre corto de la réplica (timeclock-scale-backend-2 → backend-2)."""
    if not path.exists():
        return {}
    rows = (line.split() for line in path.read_text().splitlines() if line.strip())
    return {ip: name.strip("/").removeprefix("timeclock-scale-") for ip, name in rows}


def spread(path: Path, names: dict[str, str]) -> str:
    """Peticiones que atendió cada réplica según el log del gateway (la última que se intentó)."""
    if not path.exists():
        return "-"
    served: Counter[str] = Counter()
    for line in path.read_text().splitlines():
        last = line.split(",")[-1].strip().split(":")[0]
        if last:
            served[names.get(last, last)] += 1
    total = sum(served.values()) or 1
    return " · ".join(f"{name} {count:,} ({100 * count / total:.0f} %)" for name, count in sorted(served.items()))


def connections(path: Path) -> dict[str, int]:
    """Máximos por segundo: conexiones de clientes en PostgreSQL y el pool de PgBouncer."""
    found = {"pg": 0, "cl_active": 0, "cl_waiting": 0, "sv_active": 0, "sv_total": 0, "maxwait": 0}
    if not path.exists():
        return found
    header: list[str] = []
    for line in path.read_text().splitlines():
        if line.startswith("pg="):
            found["pg"] = max(found["pg"], int(line[3:] or 0))
        elif line.startswith("database|"):
            header = line.split("|")
        elif header:
            row = dict(zip(header, line.split("|"), strict=False))
            for key in ("cl_active", "cl_waiting", "sv_active", "maxwait"):
                found[key] = max(found[key], int(row.get(key, 0) or 0))
            servers = sum(int(row.get(k, 0) or 0) for k in ("sv_active", "sv_idle", "sv_used", "sv_tested", "sv_login"))
            found["sv_total"] = max(found["sv_total"], servers)
    return found


def load_rows(folder: Path, counts: list[str], rates: list[str]) -> list[str]:
    lines = [
        "| Réplicas | Objetivo (pet/s) | Logradas | 200 OK | 503 controlado | Otros errores | p50 (ms) | p95 (ms) "
        "| p99 (ms) | Reparto por réplica (log del gateway) | Máx. conexiones PostgreSQL | PgBouncer: servidor / "
        "clientes activos / en espera / espera máx. (s) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|",
    ]
    for n in counts:
        names = replicas(folder / f"replicas-{n}.txt")
        for rate in rates:
            stage = folder / f"load-{n}-{rate}.json"
            if not stage.exists():
                continue
            summary = json.loads(stage.read_text())
            total = metric(summary, "http_reqs", "count")
            shed = metric(summary, "shed_503", "count")
            other = metric(summary, "errors_other", "count")
            conns = connections(folder / f"load-{n}-{rate}.conns")
            lines.append(
                f"| {n} | {int(rate):,} | {metric(summary, 'http_reqs', 'rate'):,.0f} | {int(total - shed - other):,} "
                f"| {int(shed):,} | {int(other):,} | {metric(summary, 'http_req_duration', 'p(50)'):.1f} "
                f"| {metric(summary, 'http_req_duration', 'p(95)'):.1f} "
                f"| {metric(summary, 'http_req_duration', 'p(99)'):.1f} "
                f"| {spread(folder / f'load-{n}-{rate}.up', names)} | {conns['pg']} "
                f"| {conns['sv_total']} / {conns['cl_active']} / {conns['cl_waiting']} / {conns['maxwait']} |"
            )
    return lines


def failover_lines(folder: Path) -> list[str]:
    path = folder / "failover.json"
    if not path.exists():
        return ["(sin datos)"]
    summary = json.loads(path.read_text())
    names = replicas(folder / "replicas-failover.txt")
    count = {
        k: int(metric(summary, k, "count")) for k in ("ok", "server_busy", "transient_first_try", "ok_after_retry")
    }
    count["failed_after_retry"] = int(metric(summary, "failed_after_retry", "count"))
    events = (folder / "failover-events.txt").read_text().strip().splitlines()
    gateway_file = folder / "failover-gateway-5xx.txt"
    gateway = gateway_file.read_text().strip() if gateway_file.exists() else ""

    def latency(key: str) -> float:
        return metric(summary, "http_req_duration", key)

    return [
        *(f"- {event}" for event in events),
        f"- Lecturas: **{count['ok']:,} correctas**, {count['server_busy']:,} descartadas a propósito "
        f"(503 SERVER_BUSY), {count['transient_first_try']:,} con una falla pasajera al primer intento, "
        f"{count['ok_after_retry']:,} correctas tras reintentar y **{count['failed_after_retry']:,} fallidas tras "
        "los reintentos**.",
        f"- Latencia: p50 {latency('p(50)'):.1f} ms, p95 {latency('p(95)'):.1f} ms, p99 {latency('p(99)'):.1f} ms, "
        f"máx. {latency('max'):.0f} ms.",
        f"- Reparto: {spread(folder / 'failover.up', names)}",
        f"- Respuestas 5xx del gateway en el periodo: {gateway or 'ninguna'}",
    ]


def deploy_lines(folder: Path, name: str) -> list[str]:
    """Una medición de despliegue en plena carga (`redeploy` o `rolling`)."""
    path = folder / f"{name}.json"
    if not path.exists():
        return ["(sin datos)"]
    summary = json.loads(path.read_text())
    events = (folder / f"{name}-events.txt").read_text().strip().splitlines()
    gateway_file = folder / f"{name}-gateway-5xx.txt"
    gateway = " ".join(gateway_file.read_text().split()) if gateway_file.exists() else ""
    count = {k: int(metric(summary, k, "count")) for k in ("ok", "transient_first_try", "ok_after_retry")}
    failed = int(metric(summary, "failed_after_retry", "count"))
    return [
        *(f"- {event}" for event in events),
        f"- Lecturas: **{count['ok']:,} correctas**, {count['transient_first_try']:,} con una falla pasajera al primer "
        f"intento, {count['ok_after_retry']:,} correctas tras reintentar y **{failed:,} fallidas tras los "
        "reintentos**.",
        f"- Latencia: p95 {metric(summary, 'http_req_duration', 'p(95)'):.1f} ms, máx. "
        f"{metric(summary, 'http_req_duration', 'max'):.0f} ms.",
        f"- Respuestas 5xx del gateway en el periodo: {gateway or 'ninguna'}",
    ]


def main() -> None:
    folder = Path(sys.argv[1])
    counts, rates = sys.argv[2].split(), sys.argv[3].split()
    ws = json.loads((folder / "ws.json").read_text()) if (folder / "ws.json").exists() else {}
    ws_names = replicas(folder / f"replicas-{counts[-1]}.txt")
    report = [
        "# Escalado horizontal (perf/scale/run.sh)",
        "",
        "## 1. Migraciones: 3 réplicas a la vez sobre una base vacía",
        "",
        "```",
        (folder / "migration-race.txt").read_text().strip() if (folder / "migration-race.txt").exists() else "",
        "```",
        "",
        "## 2. Carga por el gateway (k6, misma mezcla que perf/run.sh)",
        "",
        *load_rows(folder, counts, rates),
        "",
        "## 3. Canal WebSocket por el gateway",
        "",
        f"- {ws.get('connections', 0)} conexiones: {ws.get('codes', {})}; fallas: {len(ws.get('failures', []))}",
        f"- Reparto: {spread(folder / 'ws.up', ws_names)}",
        "",
        "## 4. Mantenimiento: una instancia a la vez",
        "",
        "```",
        (folder / "maintenance.txt").read_text().strip() if (folder / "maintenance.txt").exists() else "",
        "```",
        "",
        "## 5. Una réplica se apaga y otra se cae en plena carga",
        "",
        *failover_lines(folder),
        "",
        "## 6. Despliegue en plena carga",
        "",
        "### `docker compose up -d` (recrea TODAS las réplicas a la vez)",
        "",
        *deploy_lines(folder, "redeploy"),
        "",
        "### `scripts/deploy.sh` (primero las nuevas; las viejas se retiran una por una)",
        "",
        *deploy_lines(folder, "rolling"),
        "",
    ]
    (folder / "report.md").write_text("\n".join(report))
    print("\n".join(report))


if __name__ == "__main__":
    main()
