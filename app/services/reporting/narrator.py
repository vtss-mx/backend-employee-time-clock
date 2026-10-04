"""Narrador: la respuesta en español a partir de los datos, con hallazgos calculados.

Nunca responde «no sé»: con datos dice cuántos hay y qué destaca (el mayor, su porcentaje, el cambio
contra el periodo anterior, el promedio...); sin datos explica por qué y qué sí hay (lo calcula el
servicio ampliando la consulta).
"""

from dataclasses import dataclass
from typing import Any

from app.services.reporting.datasets import Dataset


@dataclass(frozen=True)
class Outcome:
    """Lo que salió de la consulta, para redactar la respuesta."""

    total: int
    #: Grupos (etiquetas ya legibles, valor) para el modo agrupado.
    groups: list[tuple[str, float]]
    #: Valor único (promedio, suma...) para un conteo con cálculo.
    value: float | None = None
    #: Total del periodo anterior (para comparar un conteo).
    previous: int | None = None
    #: Filas que se muestran en pantalla.
    shown: int = 0
    #: Sin filtros ni periodo (cuando no hubo resultados), para orientar.
    unfiltered: int | None = None


def amount(value: float) -> str:
    """1234 → «1,234»; 7.456 → «7.46»."""
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def counted(dataset: Dataset, total: int) -> str:
    singular, plural = dataset.noun
    return f"{amount(total)} {singular if total == 1 else plural}"


def _scope(period: str | None, conditions: list[str]) -> str:
    parts = [period] if period else []
    if conditions:
        parts.append("; ".join(conditions))
    return f" ({' · '.join(parts)})" if parts else ""


def _change(total: int, previous: int | None) -> str | None:
    if previous is None:
        return None
    if previous == 0:
        return "No hubo ninguno en el periodo anterior." if total else None
    change = (total - previous) / previous * 100
    if abs(change) < 0.5:
        return f"Igual que en el periodo anterior ({amount(previous)})."
    direction = "más" if change > 0 else "menos"
    return f"{abs(change):.0f} % {direction} que en el periodo anterior ({amount(previous)})."


def narrate(
    dataset: Dataset,
    mode: str,
    outcome: Outcome,
    *,
    period: str | None,
    conditions: list[str],
    metric_name: str | None = None,
    group_name: str | None = None,
) -> tuple[str, list[str]]:
    """(respuesta, hallazgos)."""
    scope = _scope(period, conditions)
    if outcome.total == 0:
        return _empty(dataset, outcome, scope), []
    if mode == "count" and outcome.value is not None and metric_name:
        return f"{metric_name}{scope}: {amount(round(outcome.value, 2))}, sobre {counted(dataset, outcome.total)}.", []
    if mode == "count":
        highlights = [h for h in (_change(outcome.total, outcome.previous),) if h]
        return f"Hay {counted(dataset, outcome.total)}{scope}.", highlights
    if mode == "groups":
        return _groups(dataset, outcome, scope, metric_name, group_name or "grupo")
    answer = f"Encontré {counted(dataset, outcome.total)}{scope}."
    if outcome.shown < outcome.total:
        answer += f" Te muestro {amount(outcome.shown)}; exporta a Excel para tenerlos todos."
    return answer, []


def _empty(dataset: Dataset, outcome: Outcome, scope: str) -> str:
    plural = dataset.noun[1]
    text = f"No hay {plural}{scope}."
    if outcome.unfiltered:
        text += f" Sin esas condiciones hay {counted(dataset, outcome.unfiltered)}: prueba con otro periodo o filtro."
    elif outcome.unfiltered == 0:
        text += f" Aún no hay {plural} en tu empresa."
    return text


def _groups(
    dataset: Dataset, outcome: Outcome, scope: str, metric_name: str | None, group_name: str
) -> tuple[str, list[str]]:
    groups = outcome.groups
    total = sum(value for _label, value in groups)
    top_label, top_value = groups[0]
    measured = metric_name or dataset.noun[1]
    answer = f"{measured.capitalize()} por {group_name.lower()}{scope}: {amount(len(groups))} grupos."
    highlights: list[str] = []
    if metric_name is None and total > 0:
        share = top_value / total * 100
        highlights.append(f"{top_label}: {amount(top_value)} ({share:.0f} % del total).")
        if len(groups) > 1:
            low_label, low_value = groups[-1]
            highlights.append(f"Al final: {low_label} con {amount(low_value)}.")
        average = total / len(groups)
        highlights.append(f"Promedio por {group_name.lower()}: {amount(round(average, 2))}.")
    else:
        highlights.append(f"{top_label}: {amount(round(top_value, 2))}.")
    return answer, highlights


def overview(lines: list[tuple[Dataset, int]], examples: list[str]) -> tuple[str, list[str]]:
    """Respuesta de ayuda (o cuando no se entendió): lo que hay en la empresa y qué preguntar."""
    known = [f"{counted(dataset, total)}" for dataset, total in lines if total]
    summary = f"Hoy tu empresa tiene {', '.join(known)}." if known else "Aún no hay datos en tu empresa."
    text = (
        f"{summary} Puedo responder preguntas sobre empleados, departamentos, asistencia (identificaciones, "
        "entradas, salidas y horas), registros faciales, validadores, dispositivos y llaves de la API, y "
        "exportar cualquier respuesta a Excel."
    )
    return text, [f"Prueba: «{example}»" for example in examples[:3]]


def describe_conditions(items: list[tuple[str, list[Any]]]) -> list[str]:
    """[("Departamento", ["Producción"])] → ["Departamento: Producción"]."""
    return [f"{label}: {', '.join(str(v) for v in values)}" for label, values in items]
