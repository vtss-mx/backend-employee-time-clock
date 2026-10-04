"""Validar un plan de reporte contra el catálogo y prepararlo para consultar.

Es la frontera de seguridad del asistente: el plan llega del cliente (o del intérprete) y aquí solo
pasan reportes, columnas, operaciones y valores del catálogo, con su tipo. Nada se interpola en SQL
y la empresa nunca viene en el plan (la fija el repositorio con la de la sesión).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.repositories.report_repository import Criterion, ReportQuery, Window
from app.schemas.report import ReportFilter, ReportPlan
from app.services.catalog_service import Catalogs
from app.services.reporting.datasets import BY_CODE, Column, Dataset

#: Operaciones permitidas por tipo de columna.
OPS = {
    "text": {"in", "not_in", "contains"},
    "category": {"in", "not_in"},
    "bool": {"in"},
    "number": {"in", "gt", "gte", "lt", "lte"},
    "date": {"gt", "gte", "lt", "lte"},
    "datetime": {"gt", "gte", "lt", "lte"},
}
_TEXT_MAX = 100


@dataclass(frozen=True)
class Resolved:
    dataset: Dataset
    plan: ReportPlan
    query: ReportQuery
    #: Columnas que se muestran (detalle) o grupos + total (agrupado).
    columns: list[Column]
    #: La columna de cada filtro (en el orden de `query.criteria`).
    filtered: list[Column]


def _invalid(message: str, code: str, field: str | None = None) -> UnprocessableError:
    return UnprocessableError(message, code=code, field=field)


def _dataset(code: str) -> Dataset:
    dataset = BY_CODE.get(code)
    if dataset is None:
        raise _invalid("Ese reporte no existe", "REPORT_UNKNOWN_DATASET", "dataset")
    return dataset


def _column(dataset: Dataset, code: str, field: str) -> Column:
    column = dataset.column(code)
    if column is None:
        raise _invalid(f"{dataset.name} no tiene la columna «{code}»", "REPORT_UNKNOWN_COLUMN", field)
    return column


def _allowed(column: Column, catalogs: Catalogs) -> set[Any] | None:
    """Valores válidos de una categoría (None = cualquiera)."""
    if column.kind != "category":
        return None
    allowed: set[Any] = {value.value for value in column.values}  # toda categoría tiene catálogo
    return allowed | {row["code"] for row in catalogs.entries.get(column.catalog or "", ())}


def _instant(moment: datetime) -> datetime:
    """Un momento sin zona es de la hora del negocio (como lo eligió la persona en el calendario)."""
    return moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo(settings.APP_TIMEZONE))


def _coerce(column: Column, value: Any) -> Any:
    """El valor con el tipo de la columna (o 422 si no corresponde)."""
    try:
        if column.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError
            return value
        if column.kind == "number":
            if isinstance(value, bool):
                raise ValueError
            return float(value)
        if column.kind == "date":
            return date.fromisoformat(str(value))
        if column.kind == "datetime":
            return _instant(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError) as exc:
        raise _invalid(f"Valor inválido para «{column.label}»", "REPORT_INVALID_VALUE", column.code) from exc
    text = str(value).strip()
    if not text or len(text) > _TEXT_MAX:
        raise _invalid(f"Valor inválido para «{column.label}»", "REPORT_INVALID_VALUE", column.code)
    return text


def _criterion(dataset: Dataset, item: ReportFilter, catalogs: Catalogs) -> tuple[Column, Criterion]:
    column = _column(dataset, item.column, "filters")
    if item.op not in OPS[column.kind]:
        raise _invalid(f"«{column.label}» no se puede filtrar así", "REPORT_INVALID_FILTER", column.code)
    if not item.values or (item.op not in ("in", "not_in", "contains") and len(item.values) != 1):
        raise _invalid(f"Indica un valor para «{column.label}»", "REPORT_INVALID_FILTER", column.code)
    values = tuple(_coerce(column, value) for value in item.values)
    allowed = _allowed(column, catalogs)
    if allowed is not None and not set(values) <= allowed:
        raise _invalid(f"Valor inválido para «{column.label}»", "REPORT_INVALID_VALUE", column.code)
    return column, Criterion(column.code, item.op, values, column.kind)


def _window(dataset: Dataset, plan: ReportPlan) -> Window:
    period = plan.period
    if period is None or dataset.time is None:
        return Window()
    start = _instant(period.start).astimezone(UTC) if period.start else None
    end = _instant(period.end).astimezone(UTC) if period.end else None
    if start and end and start >= end:
        raise _invalid("El periodo termina antes de empezar", "REPORT_INVALID_PERIOD", "period")
    return Window(start, end)


def _order(dataset: Dataset, plan: ReportPlan) -> tuple[str, bool]:
    """Agrupado: por el total (de más a menos) o por uno de sus grupos. Detalle: por una columna
    (por omisión la del catálogo, en su sentido)."""
    if plan.mode == "groups":
        sort = plan.sort if plan.sort in plan.group_by else "metric"
        return sort, plan.descending if plan.descending is not None else sort == "metric"
    sort = plan.sort if plan.sort and plan.sort != "metric" else dataset.sort
    _column(dataset, sort, "sort")
    if plan.descending is not None:
        return sort, plan.descending
    return sort, dataset.descending if sort == dataset.sort else False


def resolve(plan: ReportPlan, catalogs: Catalogs) -> Resolved:
    """El plan validado y normalizado, listo para el repositorio."""
    dataset = _dataset(plan.dataset)
    plan = plan.model_copy(deep=True)
    if dataset.time is None:
        plan.period = None
    filters = [_criterion(dataset, item, catalogs) for item in plan.filters]
    criteria = tuple(criterion for _column, criterion in filters)
    window = _window(dataset, plan)
    groups: list[Column] = []
    if plan.mode == "groups":
        groups = [_column(dataset, code, "group_by") for code in dict.fromkeys(plan.group_by)]
        if not groups or not all(c.group for c in groups):
            raise _invalid("Indica por qué agrupar (una columna agrupable)", "REPORT_INVALID_GROUP", "group_by")
        plan.group_by = [c.code for c in groups]
    else:
        plan.group_by = []
    metric_column = None
    if plan.metric != "count":
        metric_column = _column(dataset, plan.metric_column or "", "metric_column")
        if not metric_column.metric:
            raise _invalid(
                f"«{metric_column.label}» no se puede sumar ni promediar", "REPORT_INVALID_METRIC", "metric_column"
            )
    else:
        plan.metric_column = None
    if plan.mode == "rows":
        codes = list(dict.fromkeys(plan.columns)) or dataset.default_columns
        columns = [_column(dataset, code, "columns") for code in codes]
        plan.columns = codes
    else:
        columns = groups
        plan.columns = []
    sort, descending = _order(dataset, plan)
    query = ReportQuery(
        dataset=dataset.code,
        columns=tuple(plan.columns),
        criteria=criteria,
        window=window,
        group_by=tuple(plan.group_by),
        metric=plan.metric,
        metric_column=metric_column.code if metric_column else None,
        sort=sort,
        descending=descending,
    )
    return Resolved(dataset, plan, query, columns, [column for column, _criterion in filters])
