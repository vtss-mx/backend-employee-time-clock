"""Asistente de reportes de UNA empresa: preguntar, ver, exportar a Excel, guardar y aprender.

Todo pasa por el repositorio construido con la empresa de la sesión: el asistente no tiene manera de
ver datos de otra. Lo que aprende (vocabulario, preguntas frecuentes) también es solo de la empresa.

Evolución continua y automática:
- Crece con el sistema: cada reporte nuevo del catálogo (`datasets`) se puede preguntar, agrupar y
  exportar sin tocar nada más.
- Aprende de cada empresa: si no entendió una palabra y la persona eligió a qué datos se refería, la
  próxima vez la entiende (`feedback`); exportar o guardar una respuesta la refuerza; marcarla como
  inútil la debilita.
- Sugiere lo que la empresa más pregunta.
"""

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import as_utc, business_now, business_today
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models import AssistantQuery, SavedReport, User
from app.repositories.company_repository import CompanyRepository
from app.repositories.report_repository import Criterion, ReportQuery, ReportRepository, Window
from app.schemas.common import PageParams
from app.schemas.report import (
    CatalogColumn,
    CatalogDataset,
    ColumnOption,
    DatasetOption,
    ReportAnswer,
    ReportCatalog,
    ReportColumnRead,
    ReportPlan,
    ReportPreview,
    SavedReportCreate,
    SavedReportList,
    SavedReportRead,
)
from app.services.catalog_service import Catalogs, get_catalogs
from app.services.reporting import excel, narrator
from app.services.reporting.datasets import BY_CODE, DATASETS, Column
from app.services.reporting.interpreter import (
    METRIC_NAMES,
    Interpretation,
    Interpreter,
    Lexicon,
    display_value,
    format_hour,
)
from app.services.reporting.language import normalize
from app.services.reporting.periods import MONTHS
from app.services.reporting.planner import Resolved, resolve

logger = logging.getLogger(__name__)

#: Cuántas palabras aprende como máximo de una aclaración (lo demás suele ser ruido).
LEARN_MAX_TERMS = 5
#: Cuántas preguntas frecuentes se sugieren y de qué periodo.
POPULAR_LIMIT = 5
POPULAR_DAYS = 90
SUGGESTIONS_MAX = 8


@dataclass(frozen=True)
class ExportFile:
    content: bytes
    filename: str


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.APP_TIMEZONE)


def show(column: Column, value: Any, catalogs: Catalogs, *, excel_cell: bool = False) -> Any:
    """Un valor como lo ve la persona. En Excel, fechas y números conservan su tipo."""
    if value is None:
        return None
    if column.kind in ("bool", "category"):
        return display_value(column, bool(value) if column.kind == "bool" else str(value), catalogs)
    if column.code.endswith("_hour") or column.code == "hour":
        return format_hour(float(value)) if column.code != "hour" else f"{int(value):02d}:00"
    if column.code == "month" and isinstance(value, str) and len(value) == 7:
        return f"{MONTHS[int(value[5:]) - 1]} {value[:4]}"
    if isinstance(value, datetime):
        local = as_utc(value).astimezone(_zone())
        return local if excel_cell else local.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, date):
        return value if excel_cell else value.strftime("%d/%m/%Y")
    if isinstance(value, float):
        return round(value, 2)
    return value


class ReportAssistant:
    def __init__(self, db: Session, company_id: int, user: User) -> None:
        self.db = db
        self.company_id = company_id
        self.user = user
        self.repo = ReportRepository(db, company_id)

    # ---------- Catálogo y sugerencias ----------

    def catalog(self) -> ReportCatalog:
        catalogs = get_catalogs()
        named = {"department": self.repo.department_names(), "validator": self.repo.validator_names()}
        datasets = [
            CatalogDataset(
                code=d.code,
                name=d.name,
                description=d.description,
                time=d.time,
                examples=list(d.examples),
                columns=[
                    CatalogColumn(
                        code=c.code,
                        label=c.label,
                        kind=c.kind,
                        group=c.group,
                        default=c.default,
                        metric=c.metric,
                        options=self._options(c, catalogs, named),
                    )
                    for c in d.columns
                ],
            )
            for d in DATASETS
        ]
        return ReportCatalog(datasets=datasets, suggestions=self.suggestions())

    @staticmethod
    def _options(column: Column, catalogs: Catalogs, named: dict[str, list[str]]) -> list[ColumnOption]:
        """Valores para elegir en el constructor (categorías, sí/no y nombres de la empresa)."""
        if column.entity in named:
            return [ColumnOption(value=name, label=name) for name in named[column.entity]]
        options = {value.value: value.label for value in column.values}
        if column.catalog:
            options |= {row["code"]: str(row["name"]) for row in catalogs.entries.get(column.catalog, ())}
        return [ColumnOption(value=value, label=label) for value, label in options.items()]

    def suggestions(self) -> list[str]:
        since = datetime.now(UTC) - timedelta(days=POPULAR_DAYS)
        popular = self.repo.popular_questions(since, POPULAR_LIMIT)
        examples = [example for dataset in DATASETS for example in dataset.examples[:1]]
        return list(dict.fromkeys([*popular, *examples]))[:SUGGESTIONS_MAX]

    # ---------- Preguntar ----------

    def _lexicon(self) -> Lexicon:
        return Lexicon(
            departments=self.repo.department_names(),
            employees=self.repo.employee_names(),
            validators=self.repo.validator_names(),
            learned=self.repo.learned(),
            catalogs=get_catalogs(),
        )

    def ask(self, question: str, context: ReportPlan | None = None) -> ReportAnswer:
        """Siempre responde: con datos si entendió, con lo que hay y cómo preguntar si no."""
        interpretation = Interpreter(self._lexicon(), business_today()).interpret(question, context)
        if interpretation.plan is None:
            answer = self._overview(interpretation)
            answer.query_id = self._remember(question, None, interpretation, rows=None).id
            self.db.commit()
            return answer
        resolved = resolve(interpretation.plan, get_catalogs())
        answer = self._answer(resolved, compare=True)
        answer.understood = interpretation.understood
        answer.highlights = [*answer.highlights, *interpretation.notes]
        answer.alternatives = self._datasets(interpretation.alternatives)
        answer.export = interpretation.export
        if interpretation.unknown and interpretation.confidence < 1:
            answer.highlights.append(f"No reconocí: {', '.join(f'«{w}»' for w in interpretation.unknown[:5])}.")
        total = answer.preview.total if answer.preview else None
        answer.query_id = self._remember(question, resolved, interpretation, rows=total).id
        self.db.commit()
        return answer

    def _overview(self, interpretation: Interpretation) -> ReportAnswer:
        """Ayuda con datos reales de la empresa (y, si no entendió, qué palabras no reconoció)."""
        month_start = business_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        counts = [
            (BY_CODE["employees"], ReportQuery("employees", criteria=(Criterion("active", "in", (True,), "bool"),))),
            (BY_CODE["departments"], ReportQuery("departments")),
            (BY_CODE["attendance"], ReportQuery("attendance", window=Window(month_start.astimezone(UTC), None))),
            (BY_CODE["validators"], ReportQuery("validators")),
        ]
        lines = [(dataset, self.repo.count(query)) for dataset, query in counts]
        examples = self.suggestions()
        text, highlights = narrator.overview(lines, examples)
        if interpretation.unknown:
            words = ", ".join(f"«{w}»" for w in interpretation.unknown[:5])
            text = f"No encontré a qué datos te refieres con {words}. {text}"
        codes = interpretation.alternatives or ["attendance", "employees", "workdays"]
        return ReportAnswer(
            answer=text,
            highlights=highlights,
            alternatives=self._datasets(codes[:3]),
            suggestions=examples,
            export=interpretation.export,
        )

    @staticmethod
    def _datasets(codes: Sequence[str]) -> list[DatasetOption]:
        return [DatasetOption(code=code, name=BY_CODE[code].name) for code in codes if code in BY_CODE]

    def _remember(
        self, question: str, resolved: Resolved | None, interpretation: Interpretation, *, rows: int | None
    ) -> AssistantQuery:
        return self.repo.record(
            AssistantQuery(
                user_id=self.user.id,
                question=question.strip()[:500],
                normalized=normalize(question)[:500],
                dataset=resolved.dataset.code if resolved else None,
                plan=resolved.plan.model_dump_json() if resolved else None,
                confidence=interpretation.confidence,
                unknown_terms=" ".join([*interpretation.unknown, *interpretation.learned])[:500] or None,
                rows=rows,
            )
        )

    # ---------- Vista previa ----------

    def preview(self, plan: ReportPlan) -> ReportAnswer:
        resolved = resolve(plan, get_catalogs())
        return self._answer(resolved, compare=False)

    def _conditions(self, resolved: Resolved) -> list[str]:
        catalogs = get_catalogs()
        items = []
        for column, criterion in zip(resolved.filtered, resolved.query.criteria, strict=True):
            items.append((column.label, [display_value(column, v, catalogs) for v in criterion.values]))
        return narrator.describe_conditions(items)

    def _answer(self, resolved: Resolved, *, compare: bool) -> ReportAnswer:
        query, plan, dataset = resolved.query, resolved.plan, resolved.dataset
        period = plan.period.label if plan.period and query.window != Window() else None
        metric_column = dataset.column(query.metric_column or "")
        metric_name = f"{METRIC_NAMES[plan.metric]} de {metric_column.label.lower()}" if metric_column else None
        if plan.mode == "groups":
            preview, outcome = self._groups(resolved)
        elif plan.mode == "count":
            preview, outcome = self._count(resolved, compare=compare)
        else:
            preview, outcome = self._rows(resolved)
        if outcome.total == 0 and (query.criteria or query.window != Window()):
            outcome = narrator.Outcome(total=0, groups=[], unfiltered=self.repo.count(ReportQuery(dataset.code)))
        group_name = " y ".join(c.label for c in resolved.columns) if plan.mode == "groups" else None
        text, highlights = narrator.narrate(
            dataset,
            plan.mode,
            outcome,
            period=period,
            conditions=self._conditions(resolved),
            metric_name=metric_name,
            group_name=group_name,
        )
        return ReportAnswer(answer=text, highlights=highlights, plan=plan, preview=preview)

    def _rows(self, resolved: Resolved) -> tuple[ReportPreview, narrator.Outcome]:
        catalogs = get_catalogs()
        total = self.repo.count(resolved.query)
        rows = self.repo.rows(resolved.query, offset=0, limit=settings.REPORT_PREVIEW_ROWS)
        shown = [[show(c, v, catalogs) for c, v in zip(resolved.columns, row, strict=True)] for row in rows]
        columns = [ReportColumnRead(code=c.code, label=c.label, kind=c.kind) for c in resolved.columns]
        preview = ReportPreview(columns=columns, rows=shown, total=total, truncated=len(rows) < total)
        return preview, narrator.Outcome(total=total, groups=[], shown=len(rows))

    def _count(self, resolved: Resolved, *, compare: bool) -> tuple[ReportPreview, narrator.Outcome]:
        query = resolved.query
        total = self.repo.count(query)
        value = self.repo.aggregate(query) if query.metric_column else None
        previous = None
        if compare and query.window.start is not None and query.window.end is not None and not query.metric_column:
            length = query.window.end - query.window.start
            before = Window(query.window.start - length, query.window.start)
            previous = self.repo.count(ReportQuery(query.dataset, criteria=query.criteria, window=before))
        label = "Total" if value is None else METRIC_NAMES[resolved.plan.metric]
        shown_value = total if value is None else round(float(value), 2)
        preview = ReportPreview(
            columns=[ReportColumnRead(code="value", label=label, kind="number")],
            rows=[[shown_value]],
            total=1,
            truncated=False,
        )
        return preview, narrator.Outcome(
            total=total, groups=[], value=float(value) if value is not None else None, previous=previous
        )

    def _groups(self, resolved: Resolved) -> tuple[ReportPreview, narrator.Outcome]:
        catalogs = get_catalogs()
        limit = min(resolved.plan.limit or settings.REPORT_GROUPS_MAX, settings.REPORT_GROUPS_MAX)
        rows, total = self.repo.groups(resolved.query, limit=limit)
        metric_column = resolved.dataset.column(resolved.query.metric_column or "")
        metric_label = (
            f"{METRIC_NAMES[resolved.plan.metric]} de {metric_column.label.lower()}" if metric_column else "Cantidad"
        )
        shown = []
        groups = []
        for row in rows:
            keys = [show(c, v, catalogs) for c, v in zip(resolved.columns, row[:-1], strict=True)]
            value = float(row[-1] or 0)
            shown.append([*keys, round(value, 2) if not value.is_integer() else int(value)])
            groups.append((" · ".join("Sin dato" if k is None else str(k) for k in keys), value))
        columns = [ReportColumnRead(code=c.code, label=c.label, kind=c.kind) for c in resolved.columns]
        columns.append(ReportColumnRead(code="metric", label=metric_label, kind="number"))
        preview = ReportPreview(columns=columns, rows=shown, total=total, truncated=len(rows) < total)
        return preview, narrator.Outcome(total=len(groups), groups=groups, shown=len(groups))

    # ---------- Exportar a Excel ----------

    def export(self, plan: ReportPlan, *, query_id: int | None = None, question: str | None = None) -> ExportFile:
        catalogs = get_catalogs()
        resolved = resolve(plan, catalogs)
        dataset = resolved.dataset
        limit = settings.REPORT_EXPORT_MAX_ROWS
        if resolved.plan.mode == "rows":
            columns = [excel.SheetColumn(c.label, c.kind) for c in resolved.columns]
            rows: Iterator[Sequence[Any]] = (
                [show(c, v, catalogs, excel_cell=True) for c, v in zip(resolved.columns, row, strict=True)]
                for row in self.repo.stream(resolved.query, limit=limit)
            )
        else:
            preview, _outcome = (
                self._groups(resolved) if resolved.plan.mode == "groups" else self._count(resolved, compare=False)
            )
            columns = [excel.SheetColumn(c.label, c.kind) for c in preview.columns]
            rows = iter(preview.rows)
        company = CompanyRepository(self.db).get(self.company_id)
        summary = excel.Summary(
            title=f"Reporte: {dataset.name}",
            company=company.name if company else "",
            question=question,
            understood=[f"Periodo: {resolved.plan.period.label}"] if resolved.plan.period else [],
            generated_at=business_now(),
            limit=limit,
        )
        content = excel.build_workbook(columns, rows, summary)
        if query_id is not None:
            self._confirm(query_id, exported=True)
        today = business_today().isoformat()
        return ExportFile(content, f"reporte-{normalize(dataset.name).replace(' ', '-')}-{today}.xlsx")

    # ---------- Aprender ----------

    def _query(self, query_id: int) -> AssistantQuery:
        query = self.repo.query(query_id)
        if query is None:
            raise NotFoundError("Esa consulta no existe", code="REPORT_QUERY_NOT_FOUND")
        return query

    def _terms(self, query: AssistantQuery) -> list[str]:
        return [t for t in (query.unknown_terms or "").split() if 2 < len(t) <= 60][:LEARN_MAX_TERMS]

    def _confirm(self, query_id: int, *, exported: bool = False) -> None:
        """Exportar o guardar una respuesta confirma que se entendió: refuerza lo que no reconocía."""
        query = self._query(query_id)
        query.exported = query.exported or exported
        if query.dataset:
            self.repo.reinforce(self._terms(query), query.dataset, 1)
        self.db.commit()

    def feedback(
        self, query_id: int, *, helpful: bool | None, dataset: str | None
    ) -> tuple[list[str], ReportAnswer | None]:
        """La persona dice si sirvió o a qué datos se refería. Devuelve (lo aprendido, la nueva
        respuesta si aclaró los datos)."""
        query = self._query(query_id)
        terms = self._terms(query)
        if dataset is not None and dataset not in BY_CODE:
            raise UnprocessableError("Ese reporte no existe", code="REPORT_UNKNOWN_DATASET", field="dataset")
        if helpful is not None:
            query.helpful = helpful
            if query.dataset:
                self.repo.reinforce(terms, query.dataset, 1 if helpful else -1)
        learned: list[str] = []
        if dataset is not None:
            if query.dataset and query.dataset != dataset:
                self.repo.reinforce(terms, query.dataset, -1)
            self.repo.reinforce(terms, dataset, 2)
            learned = [f"«{term}» → {BY_CODE[dataset].name}" for term in terms]
        self.db.commit()
        if dataset is None:
            return learned, None
        answer = self.ask(query.question if terms else f"{BY_CODE[dataset].name} {query.question}")
        return learned, answer

    # ---------- Reportes guardados ----------

    def _read(self, report: SavedReport) -> SavedReportRead:
        plan = ReportPlan.model_validate(json.loads(report.plan))
        dataset = BY_CODE.get(plan.dataset)
        return SavedReportRead(
            id=report.id,
            name=report.name,
            question=report.question,
            dataset=plan.dataset,
            dataset_name=dataset.name if dataset else plan.dataset,
            plan=plan,
            created_at=report.created_at,
            last_run_at=report.last_run_at,
            runs=report.runs,
        )

    def saved(self, page: PageParams) -> SavedReportList:
        items, total = self.repo.saved_page(offset=page.offset, limit=page.size)
        return SavedReportList.of([self._read(item) for item in items], total, page)

    def save(self, data: SavedReportCreate) -> SavedReportRead:
        resolved = resolve(data.plan, get_catalogs())  # solo se guarda lo que se puede consultar
        try:
            report = self.repo.add_saved(
                SavedReport(
                    name=data.name.strip(),
                    question=data.question,
                    plan=resolved.plan.model_dump_json(),
                    created_by_id=self.user.id,
                )
            )
        except IntegrityError as exc:  # nombre repetido (sin distinguir mayúsculas), también entre dos a la vez
            self.db.rollback()
            raise ConflictError("Ya tienes un reporte con ese nombre", code="REPORT_NAME_TAKEN", field="name") from exc
        self.db.commit()
        if data.query_id is not None:
            self._confirm(data.query_id)
        return self._read(report)

    def _saved(self, report_id: int) -> SavedReport:
        report = self.repo.saved(report_id)
        if report is None:
            raise NotFoundError("Ese reporte guardado no existe", code="SAVED_REPORT_NOT_FOUND")
        return report

    def run_saved(self, report_id: int) -> ReportAnswer:
        """Lo vuelve a generar con los datos de hoy (el periodo guardado se conserva tal cual)."""
        report = self._saved(report_id)
        answer = self.preview(ReportPlan.model_validate(json.loads(report.plan)))
        report.runs += 1
        report.last_run_at = datetime.now(UTC)
        self.db.commit()
        return answer

    def delete_saved(self, report_id: int) -> None:
        self.repo.delete_saved(self._saved(report_id))
        self.db.commit()
