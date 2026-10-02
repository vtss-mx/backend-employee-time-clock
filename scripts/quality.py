"""Detector de anomalías del backend.

    python scripts/quality.py            # dentro de la imagen dev (ver Dockerfile)
    python scripts/quality.py --strict   # también falla con dependencias desactualizadas/marcadores
    python scripts/quality.py --skip=deps,audit

Verificaciones:
  Tipado ............ mypy (funciones sin anotar, tipos incompatibles) + ruff ANN (anotaciones faltantes)
  Calidad/obsoletos . ruff (UP: sintaxis/APIs obsoletas, B: bugs, S: seguridad, DTZ, C90: complejidad)
  Formato ........... ruff format --check
  Duplicidad ........ pylint duplicate-code (R0801)
  Ciclos ............ pylint cyclic-import (R0401): importaciones circulares entre módulos de app/
  Pruebas/coverage .. pytest + pytest-cov (fail_under en pyproject.toml); cualquier
                      DeprecationWarning durante las pruebas también falla
  Dependencias ...... pip list --outdated (desactualizadas), PyPI (versiones retiradas o
                      proyectos inactivos/deprecados) y pip-audit (vulnerabilidades)
  Marcadores ........ type: ignore, noqa, TODO/FIXME, print() en app/
"""

import json
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARGS = sys.argv[1:]
STRICT = "--strict" in ARGS
SKIP = {item for arg in ARGS if arg.startswith("--skip=") for item in arg.removeprefix("--skip=").split(",") if item}
TTY = sys.stdout.isatty()
results: list[tuple[str, str, str]] = []


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


def run(*cmd: str) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def record(name: str, status: str, detail: str) -> None:
    results.append((name, status, detail))
    icon = {"ok": paint("32", "✔"), "warn": paint("33", "▲"), "fail": paint("31", "✖")}[status]
    print(f"{icon} {name:<30} {detail}")


def check(key: str, name: str, cmd: list[str], summarize: Callable[[str, int], str]) -> None:
    if key in SKIP:
        record(name, "warn", "omitido")
        return
    code, out = run(*cmd)
    record(name, "ok" if code == 0 else "fail", summarize(out, code))
    if code != 0:
        print(paint("90", "\n".join(out.strip().splitlines()[-25:])))


def count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text))


print(paint("1", "\nDetección de anomalías — backend\n"))

check(
    "types",
    "Tipado (mypy)",
    [sys.executable, "-m", "mypy"],
    lambda out, code: "sin errores de tipos" if code == 0 else f"{count(r': error:', out)} errores de tipos",
)
check(
    "lint",
    "Lint (tipado/obsoletos/bugs)",
    [sys.executable, "-m", "ruff", "check", "--output-format", "concise", "app", "tests", "scripts"],
    lambda out, code: "sin problemas" if code == 0 else f"{count(r'^\S+:\d+:\d+: ', out)} problemas",
)
check(
    "format",
    "Formato (ruff format)",
    [sys.executable, "-m", "ruff", "format", "--check", "app", "tests", "scripts"],
    lambda out, code: "consistente" if code == 0 else f"{count('Would reformat', out)} archivos sin formato",
)
check(
    "duplication",
    "Duplicidad (pylint R0801)",
    [sys.executable, "-m", "pylint", "app", "--score=n"],
    lambda out, code: "0 bloques duplicados" if code == 0 else f"{count('R0801', out)} bloques duplicados",
)

check(
    "cycles",
    "Dependencias circulares",
    [sys.executable, "-m", "pylint", "app", "--disable=all", "--enable=cyclic-import", "--score=n"],
    lambda out, code: "sin ciclos de importación" if code == 0 else f"{count('R0401', out)} ciclos",
)


def coverage_summary(out: str, code: int) -> str:
    passed = re.search(r"(\d+) passed", out)
    failed = re.search(r"(\d+) failed", out)
    total = re.search(r"Total coverage: ([\d.]+)%", out) or re.search(r"TOTAL\s+.*?(\d+)%", out)
    base = f"{passed.group(1) if passed else 0} pruebas, coverage {total.group(1) if total else '?'}%"
    if failed:
        return f"{base} — {failed.group(1)} fallidas"
    return base if code == 0 else f"{base} — bajo el umbral"


if "coverage" in SKIP:
    record("Pruebas + coverage (pytest)", "warn", "omitido")
else:
    code, out = run(
        sys.executable,
        "-m",
        "pytest",
        "--cov",
        "--cov-report=term",
        "--cov-report=html:coverage_html",
        "-p",
        "no:cacheprovider",
    )
    record("Pruebas + coverage (pytest)", "ok" if code == 0 else "fail", coverage_summary(out, code))
    if code != 0:
        print(paint("90", "\n".join(out.strip().splitlines()[-25:])))
    # APIs o librerías deprecadas usadas en tiempo de ejecución (app, dependencias o pruebas).
    deprecations = sorted(set(re.findall(r"\w*DeprecationWarning: [^\n]+", out)))
    record(
        "Deprecaciones en ejecución",
        "ok" if not deprecations else "fail",
        "ninguna" if not deprecations else f"{len(deprecations)}: {deprecations[0][:110]}",
    )


def pinned_requirements() -> dict[str, str]:
    """Dependencias directas fijadas (`nombre==versión`) de requirements*.txt."""
    pins: dict[str, str] = {}
    for file in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / file).read_text().splitlines():
            match = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s#;]+)", line.strip())
            if match:
                pins[match.group(1).lower()] = match.group(2)
    return pins


def pypi_status(name: str, version: str) -> str | None:
    """Motivo si la versión fue retirada (yanked) o el proyecto está inactivo/deprecado en PyPI."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=15) as response:
        info = json.load(response)["info"]
    if info.get("yanked"):
        return f"{name} {version}: versión retirada ({info.get('yanked_reason') or 'sin motivo'})"
    if any("Development Status :: 7 - Inactive" in c for c in info.get("classifiers") or []):
        return f"{name}: proyecto inactivo"
    summary = (info.get("summary") or "").lower()
    return f"{name}: {info.get('summary')}" if re.search(r"\b(deprecated|obsolete|unmaintained)\b", summary) else None


if "deps" not in SKIP:
    code, out = run(sys.executable, "-m", "pip", "list", "--outdated", "--format=json", "--disable-pip-version-check")
    try:
        outdated = json.loads(out.strip().splitlines()[-1]) if code == 0 and out.strip() else []
    except (json.JSONDecodeError, IndexError):
        outdated = []
    required = {
        re.split(r"[=<>\[ ]", line.strip())[0].lower()
        for file in ("requirements.txt", "requirements-dev.txt")
        for line in (ROOT / file).read_text().splitlines()
        if line.strip() and not line.startswith(("#", "-r"))
    }
    direct = [p for p in outdated if p["name"].lower() in required]
    detail = ", ".join(f"{p['name']} {p['version']}→{p['latest_version']}" for p in direct[:8])
    record(
        "Dependencias desactualizadas",
        "ok" if not direct else ("fail" if STRICT else "warn"),
        "todas al día" if not direct else f"{len(direct)} directas: {detail}{'…' if len(direct) > 8 else ''}",
    )

if "deprecated" not in SKIP:
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            flagged = [r for r in pool.map(lambda item: pypi_status(*item), pinned_requirements().items()) if r]
    except OSError:
        record("Librerías deprecadas (PyPI)", "warn", "no se pudo consultar (¿sin red?)")
    else:
        record(
            "Librerías deprecadas (PyPI)",
            "ok" if not flagged else "fail",
            "ninguna retirada ni inactiva" if not flagged else "; ".join(flagged[:3]),
        )

if "audit" not in SKIP:
    code, out = run(sys.executable, "-m", "pip_audit", "-r", "requirements.txt", "--progress-spinner", "off")
    vulns = count(r"\bPYSEC-|\bGHSA-|\bCVE-", out)
    if code == 0:
        record("Vulnerabilidades (pip-audit)", "ok", "sin vulnerabilidades conocidas")
    elif vulns:
        record("Vulnerabilidades (pip-audit)", "fail", f"{vulns} vulnerabilidades")
        print(paint("90", out.strip()[-2000:]))
    else:
        record("Vulnerabilidades (pip-audit)", "warn", "no se pudo consultar (¿sin red?)")

markers = {
    "type: ignore": re.compile(r"#\s*type:\s*ignore"),
    "noqa": re.compile(r"#\s*noqa"),
    "TODO/FIXME": re.compile(r"\b(TODO|FIXME|HACK)\b"),
    "print()": re.compile(r"^\s*print\("),
}
found: dict[str, list[str]] = {key: [] for key in markers}
for file in sorted((ROOT / "app").rglob("*.py")):
    for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        for key, pattern in markers.items():
            if pattern.search(line):
                found[key].append(f"{file.relative_to(ROOT)}:{number}")
total_markers = sum(len(v) for v in found.values())
record(
    "Marcadores en el código",
    "ok" if total_markers == 0 else ("fail" if STRICT else "warn"),
    " · ".join(f"{key}: {len(items)}" for key, items in found.items()),
)
for key, items in found.items():
    if items:
        print(paint("90", f"   {key}: {', '.join(items[:5])}{'…' if len(items) > 5 else ''}"))

failed = [r for r in results if r[1] == "fail"]
warned = [r for r in results if r[1] == "warn"]
summary = (
    paint("31", f"✖ {len(failed)} verificación(es) fallida(s)")
    if failed
    else paint("32", "✔ Sin anomalías bloqueantes")
)
print(f"\n{summary}" + (paint("33", f" · {len(warned)} advertencia(s)") if warned else ""))
print("Reporte de coverage: backend/coverage_html/index.html\n")
sys.exit(1 if failed else 0)
