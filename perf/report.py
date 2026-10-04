"""Tabla de resultados de las pruebas de carga (perf/run.sh) a partir de los JSON de k6."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def stage(folder: Path, rate: int) -> dict | None:
    path = folder / f"rate-{rate}.json"
    if not path.exists():
        return None
    metrics = json.loads(path.read_text())["metrics"]

    def value(name: str, key: str, default: float = 0.0) -> float:
        return float(metrics.get(name, {}).get(key, default))

    total = value("http_reqs", "count")
    shed = value("shed_503", "count")
    other = value("errors_other", "count")
    return {
        "target": rate,
        "achieved": value("http_reqs", "rate"),
        "requests": int(total),
        "ok": int(total - shed - other),
        "shed": int(shed),
        "errors": int(other),
        "dropped": int(value("dropped_iterations", "count")),
        "p50": value("http_req_duration", "p(50)"),
        "p95": value("http_req_duration", "p(95)"),
        "p99": value("http_req_duration", "p(99)"),
    }


def main() -> None:
    folder = Path(sys.argv[1])
    rows = [r for r in (stage(folder, int(rate)) for rate in sys.argv[2:]) if r]
    lines = [
        "| Objetivo (pet/s) | Logradas (pet/s) | Peticiones | 200 OK | 503 controlado | Otros errores "
        "| No enviadas | p50 (ms) | p95 (ms) | p99 (ms) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['target']:,} | {r['achieved']:,.0f} | {r['requests']:,} | {r['ok']:,} | {r['shed']:,} "
            f"| {r['errors']:,} | {r['dropped']:,} | {r['p50']:.1f} | {r['p95']:.1f} | {r['p99']:.1f} |"
        )
    report = "\n".join(lines)
    (folder / "report.md").write_text(report + "\n")
    (folder / "report.json").write_text(json.dumps(rows, indent=2))
    print(report)


if __name__ == "__main__":
    main()
