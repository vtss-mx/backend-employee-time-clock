"""Intérprete: una pregunta en español (y la conversación) → un plan de reporte.

Sin inteligencia externa: reconoce, en este orden, lo que la pregunta dice con certeza y lo marca
como usado para que nada se interprete dos veces:

1. Si pide el archivo («en Excel», «expórtalo») o ayuda («¿qué puedes hacer?»).
2. El periodo («ayer», «del 1 al 15 de marzo») — `periods`.
3. Nombres propios de la empresa: departamentos, empleados (o su número) y validadores.
4. De qué datos habla: palabras del catálogo (con errores de dedo) y lo que la empresa enseñó.
5. Qué quiere saber: cuántos, «por» qué agrupar, los que más / menos, promedios y sumas.
6. Filtros por valor («fallidas», «sin registro facial», «más de 10 empleados», «después de las 9»).
7. Columnas extra («con su correo»), orden («los más recientes»).

Una pregunta que no menciona datos sigue la conversación anterior («¿y por departamento?»). Lo que
no reconoce queda en `unknown` para aprender si la persona aclara (`service.feedback`).
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, date

from app.schemas.report import ReportFilter, ReportPeriod, ReportPlan
from app.services.catalog_service import Catalogs
from app.services.reporting.datasets import BY_CODE, DATASETS, Column, Dataset
from app.services.reporting.language import (
    content_words,
    find_phrase,
    normalize,
    number_at,
    phrase_stems,
    similar,
    stem,
)
from app.services.reporting.periods import find_period

_EXPORT: tuple[str, ...] = (
    "excel",
    "exporta",
    "exportar",
    "exportalo",
    "exportala",
    "exportame",
    "descarga",
    "descargar",
)
_EXPORT += ("descargalo", "descargala", "xlsx", "hoja de calculo", "archivo", "bajar")
_HELP = re.compile(r"^(ayuda|help|hola|que (puedes|sabes) hacer|que puedes|como (funciona|te uso)|que haces|buen[oa]s)")
_COUNT = ("cuanto", "cuantos", "cuantas", "cuanta", "numero de", "total de", "cantidad de", "cuenta", "contar")
_MORE, _LESS = ("mas", "mayor", "mayores"), ("menos", "menor", "menores")
_METRICS = {"promedio": "avg", "media": "avg", "suma": "sum", "sumar": "sum", "maximo": "max", "minimo": "min"}
_LATE = ("tarde", "retardo", "retardos", "retraso", "retrasos", "impuntual")
_RECENT, _OLDEST = (
    ("reciente", "recientes", "ultimo", "ultimos", "ultima", "ultimas"),
    ("antiguo", "antiguos", "primero", "primeros"),
)
_CLEAR = ("todos", "todas", "sin filtro", "sin filtros", "quita los filtros", "quitar filtros")
_DETAIL = ("detalle", "lista", "listado", "cuales", "quienes", "muestrame", "ver")
_TIME_GROUPS = {"day", "week", "month", "hour"}


@dataclass(frozen=True)
class Lexicon:
    """Lo propio de la empresa para entender sus preguntas (nunca de otra empresa)."""

    departments: list[str]
    employees: list[tuple[str, str]]
    validators: list[str]
    #: {palabra (raíz): {reporte: aciertos}} aprendido de sus aclaraciones.
    learned: dict[str, dict[str, int]]
    catalogs: Catalogs


@dataclass
class Interpretation:
    plan: ReportPlan | None = None
    dataset: Dataset | None = None
    confidence: float = 0.0
    understood: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    #: Palabras que entendió gracias a lo que aprendió de la empresa (se refuerzan o se debilitan).
    learned: list[str] = field(default_factory=list)
    export: bool = False
    help: bool = False
    notes: list[str] = field(default_factory=list)


class _Question:
    """La pregunta en palabras y raíces, con lo que ya se interpretó."""

    def __init__(self, text: str) -> None:
        self.text = normalize(text)
        self.words = self.text.split()
        self.stems = [stem(w) for w in self.words]
        self.used = [False] * len(self.words)

    def find(self, phrase: str | tuple[str, ...], *, fuzzy: bool = False, free: bool = True) -> int:
        """Posición de la frase entre las palabras (aún no usadas, si `free`); -1 si no está."""
        stems = phrase_stems(phrase) if isinstance(phrase, str) else phrase
        start = 0
        while start <= len(self.stems) - len(stems):
            found = find_phrase(self.stems[start:], stems, fuzzy=fuzzy)
            if found < 0:
                return -1
            position = start + found
            if not free or not any(self.used[position : position + len(stems)]):
                return position
            start = position + 1
        return -1

    def take(self, phrase: str | tuple[str, ...], *, fuzzy: bool = False) -> bool:
        stems = phrase_stems(phrase) if isinstance(phrase, str) else phrase
        position = self.find(stems, fuzzy=fuzzy)
        if position < 0:
            return False
        self.mark(position, len(stems))
        return True

    def has(self, *phrases: str) -> bool:
        return any(self.find(p, free=False) >= 0 for p in phrases)

    def mark(self, position: int, length: int) -> None:
        for i in range(position, min(position + length, len(self.used))):
            self.used[i] = True

    def mark_span(self, span: tuple[int, int]) -> None:
        """Marca las palabras que caen dentro de un tramo del texto normalizado."""
        offset = 0
        for i, word in enumerate(self.words):
            if offset < span[1] and offset + len(word) > span[0]:
                self.used[i] = True
            offset += len(word) + 1

    def unknown(self) -> list[str]:
        free = [w for w, used in zip(self.stems, self.used, strict=True) if not used]
        return list(dict.fromkeys(content_words(free)))


def _value_phrases(column: Column, catalogs: Catalogs) -> list[tuple[tuple[str, ...], object, str]]:
    """(frase, valor, nombre) de cada valor de la columna: sus sinónimos y su nombre en el catálogo."""
    phrases = [(phrase_stems(word), value.value, value.label) for value in column.values for word in value.words]
    if column.catalog:
        for row in catalogs.entries.get(column.catalog, ()):
            phrases.append((phrase_stems(str(row["name"])), row["code"], str(row["name"])))
    return [p for p in phrases if p[0]]


class Interpreter:
    def __init__(self, lexicon: Lexicon, today: date) -> None:
        self.lexicon = lexicon
        self.today = today

    def interpret(self, question: str, context: ReportPlan | None = None) -> Interpretation:
        q = _Question(question)
        result = Interpretation()
        result.export = any(q.take(word) for word in _EXPORT)
        if _HELP.match(q.text) and not context:
            result.help = True
            return result
        found = find_period(q.text, self.today)
        if found is not None:
            q.mark_span(found.span)
        entities = self._entities(q)
        scores = self._scores(q, grouping_only=context is not None)
        dataset, confidence = self._choose(q, scores, entities, context, found is not None, result)
        ranking = sorted(scores.items(), key=lambda item: (-item[1][0], item[1][1]))
        result.alternatives = [code for code, (score, _first) in ranking if score > 0]
        if dataset is None:  # (con conversación nunca pasa: se sigue con sus datos)
            result.unknown = q.unknown()
            return result
        result.dataset = dataset
        result.confidence = confidence
        result.alternatives = [code for code in result.alternatives if code != dataset.code][:3]
        follow_up = context is not None and context.dataset == dataset.code and confidence <= 0.3
        plan = context.model_copy(deep=True) if follow_up and context else ReportPlan(dataset=dataset.code)
        if follow_up:
            result.understood.append("Sigo con la consulta anterior")
            if any(q.take(word) for word in _CLEAR):
                plan.filters = []
        if found is not None and dataset.time:
            plan.period = ReportPeriod(
                start=found.period.start.astimezone(UTC) if found.period.start else None,
                end=found.period.end.astimezone(UTC),
                label=found.period.label,
            )
        elif found is not None:
            result.notes.append(f"{dataset.name} no tiene fecha: no se aplica el periodo «{found.period.label}».")
        self._entity_filters(dataset, entities, plan, result)
        self._value_filters(q, dataset, plan)
        self._comparisons(q, dataset, plan, result)
        self._shape(q, dataset, plan, result, follow_up)
        self._columns_and_order(q, dataset, plan)
        result.plan = plan
        result.unknown = q.unknown()
        self._describe(dataset, plan, result)
        return result

    # ---------- Nombres propios ----------

    def _entities(self, q: _Question) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}

        def match(kind: str, names: list[str]) -> None:
            for name in sorted(set(names), key=len, reverse=True):
                stems = phrase_stems(name)
                single_long = len(stems) == 1 and len(stems[0]) >= 7
                if stems and q.take(stems, fuzzy=single_long):
                    found.setdefault(kind, []).append(name)

        match("department", self.lexicon.departments)
        match("validator", self.lexicon.validators)
        names = [name for name, _number in self.lexicon.employees]
        match("employee", [n for n in names if len(phrase_stems(n)) >= 2])
        numbers = {normalize(number): name for name, number in self.lexicon.employees}
        for i, word in enumerate(q.words):
            if not q.used[i] and word in numbers and any(ch.isdigit() for ch in word):
                found.setdefault("employee", []).append(numbers[word])
                q.used[i] = True
        if "employee" not in found:  # «de Juan Pérez» sin el segundo apellido: nombre + apellido
            for name in names:
                parts = phrase_stems(name)
                if len(parts) >= 3 and q.find((parts[0], parts[1])) >= 0:
                    q.take((parts[0], parts[1]))
                    found.setdefault("employee", []).append(name)
        return found

    # ---------- De qué datos se trata ----------

    def _scores(self, q: _Question, *, grouping_only: bool = False) -> dict[str, tuple[int, int]]:
        """(puntos, primera posición) de cada reporte según las palabras de la pregunta.

        Lo que va después de «por» / «cada» dice cómo agrupar, no de qué datos se trata: vale poco
        («promedio de confianza por validador» son identificaciones) y, en una conversación, nada
        («¿y por departamento?»). Con empate gana el que se nombró primero (el sujeto)."""
        scores: dict[str, tuple[int, int]] = {}
        content = content_words([s for s, used in zip(q.stems, q.used, strict=True) if not used])
        for dataset in DATASETS:
            score, first = 0, len(q.words)
            for word in dataset.words:
                stems = phrase_stems(word)
                position = q.find(stems, fuzzy=True)
                if position < 0:
                    continue
                grouping = position > 0 and q.words[position - 1] in ("por", "cada")
                score += (0 if grouping_only else 1) if grouping else 3 * len(stems)
                first = min(first, position) if not grouping else first
            for column in dataset.columns:
                if any(q.find(word) >= 0 for word in column.words):
                    score += 1
            for word in content:
                hits = self.lexicon.learned.get(word, {}).get(dataset.code, 0)
                score += 2 * min(hits, 3)
            scores[dataset.code] = (score, first)
        return scores

    def _choose(
        self,
        q: _Question,
        scores: dict[str, tuple[int, int]],
        entities: dict[str, list[str]],
        context: ReportPlan | None,
        has_period: bool,
        result: Interpretation,
    ) -> tuple[Dataset | None, float]:
        ranked = sorted(DATASETS, key=lambda d: (-scores[d.code][0], scores[d.code][1]))
        best, second = scores[ranked[0].code][0], scores[ranked[1].code][0]
        if best >= 3:
            dataset = ranked[0]
            for word in dataset.words:  # lo que eligió el reporte ya está interpretado
                q.take(word, fuzzy=True)
            self._take_learned(q, dataset, result)
            return dataset, 1.0 if best >= 2 * max(second, 1) else 0.6
        if context is not None and context.dataset in BY_CODE:
            return BY_CODE[context.dataset], 0.3  # sin datos nuevos: sigue la conversación
        if best > 0:
            self._take_learned(q, ranked[0], result)
            return ranked[0], 0.4
        if has_period or q.has(*_LATE):
            # Con un periodo y nada más, en un reloj checador se pregunta por la asistencia.
            return BY_CODE["workdays" if q.has(*_LATE) else "attendance"], 0.4
        if entities:
            return BY_CODE["employees"], 0.4
        return None, 0.0

    def _take_learned(self, q: _Question, dataset: Dataset, result: Interpretation) -> None:
        for word, datasets in self.lexicon.learned.items():
            if datasets.get(dataset.code) and q.take((word,)):
                result.learned.append(word)

    # ---------- Filtros ----------

    def _set_filter(self, plan: ReportPlan, column: str, op: str, values: list[object]) -> None:
        plan.filters = [f for f in plan.filters if not (f.column == column and f.op == op)]
        plan.filters.append(ReportFilter(column=column, op=op, values=values))

    def _entity_filters(
        self, dataset: Dataset, entities: dict[str, list[str]], plan: ReportPlan, result: Interpretation
    ) -> None:
        for kind, names in entities.items():
            column = next((c for c in dataset.columns if c.entity == kind), None)
            if column is None:
                result.notes.append(f"{dataset.name} no se puede filtrar por {', '.join(names)}.")
                continue
            self._set_filter(plan, column.code, "in", list(dict.fromkeys(names)))

    def _value_filters(self, q: _Question, dataset: Dataset, plan: ReportPlan) -> None:
        candidates = [
            (phrase, value, column)
            for column in dataset.columns
            if column.kind in ("category", "bool")
            for phrase, value, _label in _value_phrases(column, self.lexicon.catalogs)
        ]
        chosen: dict[str, list[object]] = {}
        for phrase, value, column in sorted(candidates, key=lambda c: -len(c[0])):
            position = q.find(phrase)
            if position < 0:
                continue
            negated = position > 0 and q.words[position - 1] in ("no", "sin") and not q.used[position - 1]
            q.mark(position - negated, len(phrase) + negated)
            if column.kind == "bool" and negated:
                value = not value
            values = chosen.setdefault(column.code, [])
            if value not in values:
                values.append(value)
        for code, values in chosen.items():
            self._set_filter(plan, code, "in", values)

    def _numeric_column(self, dataset: Dataset, word: str | None) -> Column | None:
        numeric = [c for c in dataset.columns if c.kind == "number"]
        if word:
            for column in numeric:
                if any(stem(word) in phrase_stems(w) for w in column.words) or similar(
                    stem(word), stem(column.label.lower())
                ):
                    return column
        return None

    def _comparisons(self, q: _Question, dataset: Dataset, plan: ReportPlan, result: Interpretation) -> None:
        """«con más de 10 empleados», «después de las 9:30», «antes de las 8»."""
        for match in re.finditer(r"\b(mas|menos|mayor|menor)\s+(?:de|que|a)\s+(\d+(?:\.\d+)?)\s*(\w+)?", q.text):
            column = self._numeric_column(dataset, match.group(3))
            if column is None:
                continue
            op = "gt" if match.group(1) in ("mas", "mayor") else "lt"
            self._set_filter(plan, column.code, op, [float(match.group(2))])
            q.mark_span(match.span())
        clock = re.search(
            r"\b(despues|antes)\s+de\s+las?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|de la manana|de la tarde|de la noche)?",
            q.text,
        )
        if clock and dataset.code in ("workdays", "attendance"):
            hour = int(clock.group(2)) % 24 + int(clock.group(3) or 0) / 60
            if clock.group(4) in ("pm", "de la tarde", "de la noche") and hour < 12:
                hour += 12
            op = "gt" if clock.group(1) == "despues" else "lt"
            leaving = q.has("salida", "salieron", "salio", "se fueron", "se fue")
            target = "hour" if dataset.code == "attendance" else ("exit_hour" if leaving else "entry_hour")
            self._set_filter(plan, target, op, [hour])
            q.mark_span(clock.span())
            timed = ("exit" if leaving else "entry") if dataset.code == "workdays" else "occurred_at"
            plan.sort, plan.descending = timed, op == "gt"
        elif dataset.code == "workdays" and q.has(*_LATE):
            result.notes.append(
                "Para saber quién llegó tarde dime a partir de qué hora; por ejemplo: «¿quién llegó después de las "
                "9:00 ayer?». Mientras, te muestro la hora de entrada de cada quien, la más tardía primero."
            )
            plan.sort, plan.descending = "entry", True

    # ---------- Forma: detalle, conteo, grupos, los que más ----------

    def _group_column(self, q: _Question, dataset: Dataset, start: int) -> Column | None:
        for column in dataset.columns:
            if not column.group:
                continue
            for word in column.words:
                stems = phrase_stems(word)
                if q.stems[start : start + len(stems)] == list(stems):
                    q.mark(start - 1, len(stems) + 1)
                    return column
        return None

    def _groups(self, q: _Question, dataset: Dataset) -> list[Column]:
        """Columnas pedidas con «por X» / «por cada X» / «de cada X» / «por qué» (motivo)."""
        found: list[Column] = []
        for i, word in enumerate(q.words[:-1]):
            if word not in ("por", "cada") or q.used[i]:
                continue
            if word == "por" and q.words[i + 1] in ("favor", "ciento"):
                continue
            reason = dataset.column("reason")
            if word == "por" and q.words[i + 1] == "que" and reason is not None:
                q.mark(i, 2)
                found.append(reason)
                continue
            start = i + 2 if q.words[i + 1] == "cada" else i + 1
            column = self._group_column(q, dataset, start)
            if column is not None and column not in found:
                found.append(column)
        return found[:2]

    def _top(self, q: _Question) -> tuple[bool, bool, int | None]:
        """(¿pide los que más/menos?, ¿de más a menos?, cuántos)."""
        more = any(q.find(w, free=False) >= 0 for w in _MORE)
        less = any(q.find(w, free=False) >= 0 for w in _LESS)
        if not (more or less):
            return False, True, None
        amount = None
        for i, word in enumerate(q.words):
            value = number_at(word)
            if (
                value
                and 0 < value <= 1000
                and i > 0
                and q.words[i - 1] in ("los", "las", "top", "primeros", "primeras")
            ):
                amount = value
                q.used[i] = True
        asks_who = q.has("quien", "quienes", "que", "cual", "cuales", "top")
        return (asks_who or amount is not None), not less or more, amount

    def _shape(self, q: _Question, dataset: Dataset, plan: ReportPlan, result: Interpretation, follow_up: bool) -> None:
        groups = self._groups(q, dataset)
        top, descending, amount = self._top(q)
        metric = next((_METRICS[w] for w in q.words if w in _METRICS), None)
        if metric is None and dataset.code == "workdays" and q.has("horas", "tiempo"):
            metric = "sum"  # «horas trabajadas por empleado»: las horas se suman, no se cuentan
        metric_column = self._metric_column(q, dataset) if metric else None
        if metric and metric_column is None:
            metric = None
        if top and not groups:
            entity = next((c for c in dataset.columns if c.group and c.entity), None)
            asked = next(
                (c for c in dataset.columns if c.group and any(q.find(w, free=False) >= 0 for w in c.words)), None
            )
            chosen = asked or entity
            groups = [chosen] if chosen is not None else []
        if groups:
            plan.mode, plan.group_by = "groups", [c.code for c in groups]
            plan.metric, plan.metric_column = (metric or "count"), metric_column.code if metric_column else None  # type: ignore[assignment]
            if all(c.code in _TIME_GROUPS for c in groups) and not top:
                plan.sort, plan.descending = groups[0].code, False  # en orden cronológico
            else:
                plan.sort, plan.descending = "metric", descending
            plan.limit = amount or (10 if top else None)
            return
        if metric and metric_column is not None:
            plan.mode, plan.metric, plan.metric_column = "count", metric, metric_column.code  # type: ignore[assignment]
            return
        if any(q.take(word) for word in _COUNT):
            plan.mode = "count"
        elif follow_up and any(q.take(word) for word in _DETAIL):
            plan.mode, plan.group_by = "rows", []
        elif not follow_up:
            plan.mode = "rows"

    def _metric_column(self, q: _Question, dataset: Dataset) -> Column | None:
        metrics = [c for c in dataset.columns if c.metric]
        for column in metrics:
            if any(q.find(word, free=False) >= 0 for word in column.words):
                return column
        return metrics[0] if len(metrics) == 1 or (metrics and dataset.code == "workdays") else None

    # ---------- Columnas y orden ----------

    def _columns_and_order(self, q: _Question, dataset: Dataset, plan: ReportPlan) -> None:
        if plan.mode != "rows":
            return
        extra = [
            c.code
            for c in dataset.columns
            if not c.default and c.kind != "number" and any(q.find(w, free=False) >= 0 for w in c.words)
        ]
        if extra:
            plan.columns = list(dict.fromkeys([*(plan.columns or dataset.default_columns), *extra]))
        if plan.sort is not None:
            return
        if dataset.time and any(q.find(w, free=False) >= 0 for w in _RECENT):
            plan.sort, plan.descending = dataset.time, True
        elif dataset.time and any(q.find(w, free=False) >= 0 for w in _OLDEST):
            plan.sort, plan.descending = dataset.time, False

    # ---------- Cómo se entendió ----------

    def _describe(self, dataset: Dataset, plan: ReportPlan, result: Interpretation) -> None:
        catalogs = self.lexicon.catalogs
        result.understood.append(f"Datos: {dataset.name}")
        if plan.period is not None:
            result.understood.append(f"Periodo: {plan.period.label}")
        for item in plan.filters:
            column = dataset.column(item.column)
            if column is None:
                continue
            values = ", ".join(display_value(column, value, catalogs) for value in item.values)
            symbol = {"in": "", "not_in": "no ", "contains": "contiene ", "gt": "más de ", "gte": "desde "}
            symbol |= {"lt": "menos de ", "lte": "hasta "}
            result.understood.append(f"{column.label}: {symbol[item.op]}{values}")
        if plan.mode == "count":
            result.understood.append(
                "Cuántos hay"
                if plan.metric == "count"
                else f"{METRIC_NAMES[plan.metric]} de {_label(dataset, plan.metric_column)}"
            )
        if plan.mode == "groups":
            names = " y ".join(_label(dataset, code) for code in plan.group_by)
            result.understood.append(f"Agrupado por {names.lower()}")
            if plan.metric != "count":
                result.understood.append(f"{METRIC_NAMES[plan.metric]} de {_label(dataset, plan.metric_column)}")
            if plan.limit:
                result.understood.append(f"Los {plan.limit} con {'más' if plan.descending else 'menos'}")


METRIC_NAMES = {"count": "Cantidad", "sum": "Suma", "avg": "Promedio", "min": "Mínimo", "max": "Máximo"}


def _label(dataset: Dataset, code: str | None) -> str:
    column = dataset.column(code or "")
    return column.label if column else (code or "")


def display_value(column: Column, value: object, catalogs: Catalogs) -> str:
    """El valor como lo ve la persona: el nombre del catálogo, «Sí/No» o la etiqueta del valor."""
    known = next((v.label for v in column.values if v.value == value), None)
    if known:  # sí/no y sinónimos: toda columna sí/no define sus valores (prueba del catálogo)
        return known
    if column.catalog:
        return catalogs.name(column.catalog, str(value))
    if isinstance(value, float):
        return format_hour(value) if column.code.endswith("_hour") or column.code == "hour" else f"{value:g}"
    return str(value)


def format_hour(value: float) -> str:
    """9.5 → 9:30."""
    hours = int(value)
    return f"{hours}:{round((value - hours) * 60):02d}"
