"""Observabilidad de rendimiento (esquema `ops`): guardado en lotes, resúmenes del mantenimiento y lecturas del ADMIN.

- Guardado: UN upsert por lote que SUMA (contadores, tiempos y cubetas del histograma; el máximo se queda con el
  mayor). Atómico entre réplicas: dos lotes del mismo minuto nunca se pisan.
- Resúmenes: `INSERT ... SELECT ... GROUP BY` de un rango cerrado (una hora desde los minutos, un día desde las
  horas) que REEMPLAZA la fila: repetirlo da lo mismo (idempotente), así el mantenimiento puede volver a sumar las
  horas recientes en cada vuelta sin duplicar nada.
- Lecturas: siempre de UN tipo (o de una lista fija de tipos, `kind IN (...)`, que PostgreSQL recorre como varios
  rangos de la llave primaria) en un rango de tiempo acotado; las sumas y los agrupamientos en la base, nunca filas
  en Python. Las filas que salen están acotadas por el periodo (≤ 1 440 minutos, ≤ 168 horas, ≤ 90 días) o por los
  nombres (rutas, funciones y pantallas: los acota el código, no el tráfico).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import ColumnElement, case, func, literal, select, text, true, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.histogram import BOUNDS_MS, COLUMNS
from app.models import ErrorOccurrence, PerfDay, PerfHour, PerfKind, PerfMinute, SlowAlertStatus, SlowRequestAlert
from app.models.performance import PerfCounters
from app.repositories.aggregates import affected_rows, dialect_insert
from app.repositories.search import contains_text, search_term

#: Contadores que se SUMAN (el máximo aparte).
SUMMED: Final = (
    "count",
    "errors",
    "client_errors",
    "total_ms",
    "db_ms",
    "db_queries",
    "bytes_in",
    "bytes_out",
    *COLUMNS,
)
#: Todos los tipos (la depuración y los resúmenes recorren la llave primaria tipo por tipo).
ALL_KINDS: Final = tuple(kind.value for kind in PerfKind)
#: Los Web Vitals y las tareas largas de cada pantalla.
WEB_SCREEN_KINDS: Final = (
    PerfKind.WEB_LCP,
    PerfKind.WEB_INP,
    PerfKind.WEB_CLS,
    PerfKind.WEB_FCP,
    PerfKind.WEB_TTFB,
    PerfKind.WEB_LONG_TASK,
)
#: Los Web Vitals (sin las tareas largas): sus muestras ordenan las pantallas.
VITAL_KINDS: Final = WEB_SCREEN_KINDS[:-1]


@dataclass(frozen=True)
class Grain:
    """Un grano de las tablas de rendimiento: su modelo y su columna de tiempo."""

    model: type[PerfCounters]
    time: InstrumentedAttribute[Any]


MINUTES: Final = Grain(PerfMinute, PerfMinute.minute)
HOURS: Final = Grain(PerfHour, PerfHour.hour)
DAYS: Final = Grain(PerfDay, PerfDay.day)


def sums(model: type[PerfCounters]) -> list[ColumnElement[Any]]:
    """SUM de cada contador y el máximo, con su nombre (el de la columna)."""
    columns: list[ColumnElement[Any]] = [func.sum(getattr(model, name)).label(name) for name in SUMMED]
    columns.append(func.max(model.max_ms).label("max_ms"))
    return columns


def _bucket_bound(model: type[PerfCounters], q: float) -> ColumnElement[Any]:
    """El límite de la cubeta donde cae el percentil `q` de un grupo (sin interpolar): ordena "por p95" en la base.
    Lo exacto (interpolado) lo calcula `app/core/histogram.py` para lo que se muestra."""
    total = func.sum(model.count)
    whens = []
    running: ColumnElement[Any] | None = None
    for column, bound in zip(COLUMNS, BOUNDS_MS, strict=False):
        running = func.sum(getattr(model, column)) if running is None else running + func.sum(getattr(model, column))
        whens.append((running >= total * q, bound))
    return case(*whens, else_=BOUNDS_MS[-1] + 1)


def _order(model: type[PerfCounters], sort: str) -> ColumnElement[Any]:
    """Expresión del orden de un listado (la mayor primero)."""
    count = func.sum(model.count)
    return {
        "impact": func.sum(model.total_ms),
        # La cubeta del p95 manda y, dentro de la misma cubeta, el promedio (multiplicar por un real: un entero
        # se desbordaría en PostgreSQL).
        "p95": _bucket_bound(model, 0.95) * 1e6 + func.sum(model.total_ms) / count,
        "mean": func.sum(model.total_ms) / count,
        "max": func.max(model.max_ms),
        "count": count,
        "errors": func.sum(model.errors),
    }[sort]


class PerformanceRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------- Guardado en lotes (hilo de los observadores) ----------

    def add_minutes(self, rows: Sequence[dict[str, Any]]) -> None:
        """Suma un lote al minuto de cada fila: la nueva se inserta y la que ya existe suma lo nuevo."""
        stmt = dialect_insert(self.db, PerfMinute)
        table = PerfMinute.__table__.c
        updates: dict[str, Any] = {name: table[name] + stmt.excluded[name] for name in SUMMED}
        updates["max_ms"] = case((stmt.excluded.max_ms > table.max_ms, stmt.excluded.max_ms), else_=table.max_ms)
        self.db.execute(stmt.on_conflict_do_update(index_elements=["kind", "minute", "name"], set_=updates), rows)

    def add_slow(self, rows: Sequence[dict[str, Any]]) -> None:
        """Suma un lote de peticiones lentas a la alerta de su ruta (regla 18). La última ocurrencia (y su muestra)
        es la más reciente; una alerta RESUELTA que vuelve a ocurrir se reabre sola (cuenta `reopened`)."""
        stmt = dialect_insert(self.db, SlowRequestAlert)
        table, new = SlowRequestAlert.__table__.c, stmt.excluded
        newer = new.last_seen_at >= table.last_seen_at
        resolved = table.status == SlowAlertStatus.RESOLVED.value

        def latest(column: str) -> Any:
            return case((newer, new[column]), else_=table[column])

        updates: dict[str, Any] = {
            "count": table.count + new.count,
            "total_ms": table.total_ms + new.total_ms,
            "max_ms": case((new.max_ms > table.max_ms, new.max_ms), else_=table.max_ms),
            "first_seen_at": case(
                (new.first_seen_at < table.first_seen_at, new.first_seen_at), else_=table.first_seen_at
            ),
            **{column: latest(column) for column in ("last_ms", "last_seen_at", "last_trace_id", "last_status")},
            **{column: latest(column) for column in ("sample", "threshold_ms")},
            "status": case((resolved, SlowAlertStatus.OPEN.value), else_=table.status),
            "reopened": table.reopened + case((resolved, 1), else_=0),
            "opened_at": case((resolved, new.opened_at), else_=table.opened_at),
            "status_changed_at": case((resolved, new.opened_at), else_=table.status_changed_at),
            "status_changed_by": case((resolved, None), else_=table.status_changed_by),
        }
        self.db.execute(stmt.on_conflict_do_update(index_elements=["route"], set_=updates), rows)

    # ---------- Resúmenes (mantenimiento) ----------

    def rollup_hour(self, hour: datetime) -> int:
        """Vuelve a sumar una hora desde sus minutos (reemplaza sus filas: idempotente); dice cuántas escribió."""
        return self._rollup(MINUTES, HOURS, hour, hour + timedelta(hours=1), hour)

    def rollup_day(self, day: date, start: datetime, end: datetime) -> int:
        """Vuelve a sumar un día del negocio [start, end) desde sus horas (reemplaza sus filas: idempotente)."""
        return self._rollup(HOURS, DAYS, start, end, day)

    def _rollup(self, source: Grain, target: Grain, start: datetime, end: datetime, bucket: Any) -> int:
        model = source.model
        rows = (
            select(model.kind, literal(bucket, target.time.type), model.name, *sums(model))
            .where(model.kind.in_(ALL_KINDS), source.time >= start, source.time < end)
            .group_by(model.kind, model.name)
        )
        stmt = dialect_insert(self.db, target.model)
        names = ["kind", target.time.key, "name", *SUMMED, "max_ms"]
        stmt = stmt.from_select(names, rows)
        replace = {name: stmt.excluded[name] for name in (*SUMMED, "max_ms")}
        keys = ["kind", target.time.key, "name"]
        # RETURNING cuenta lo escrito igual en PostgreSQL y SQLite (el `rowcount` de un INSERT ... SELECT no llega).
        upsert = stmt.on_conflict_do_update(index_elements=keys, set_=replace).returning(target.model.kind)
        return len(self.db.execute(upsert).all())

    # ---------- Lecturas del ADMIN ----------

    def series(self, grain: Grain, kinds: Sequence[str], start: Any, end: Any, *, name: str | None = None) -> list[Any]:
        """Contadores por instante del grano (todos los nombres sumados, o uno solo), en orden."""
        model = grain.model
        stmt = select(grain.time.label("at"), *sums(model)).where(
            model.kind.in_(kinds), grain.time >= start, grain.time < end
        )
        if name is not None:
            stmt = stmt.where(model.name == name)
        return list(self.db.execute(stmt.group_by(grain.time).order_by(grain.time)).all())

    def metrics_page(
        self,
        grain: Grain,
        kind: str,
        start: Any,
        end: Any,
        *,
        sort: str,
        search: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Any], int, float]:
        """Nombres de un tipo con sus contadores del periodo en el orden pedido, cuántos son y el tiempo total del
        tipo (para el porcentaje de cada uno), en UNA consulta (`... OVER ()`). Una página más allá del final no trae
        filas: solo entonces se cuenta aparte."""
        model = grain.model
        filters = self._metric_filters(grain, kind, start, end, search)
        stmt = (
            select(
                model.name,
                *sums(model),
                func.count().over().label("names"),
                func.sum(func.sum(model.total_ms)).over().label("kind_total_ms"),
            )
            .where(*filters)
            .group_by(model.name)
            .order_by(_order(model, sort).desc(), model.name)
            .offset(offset)
            .limit(limit)
        )
        rows = list(self.db.execute(stmt).all())
        if rows:
            return rows, int(rows[0].names), float(rows[0].kind_total_ms or 0)
        total = self.db.scalar(select(func.count(func.distinct(model.name))).where(*filters)) if offset else 0
        return [], int(total or 0), 0.0

    @staticmethod
    def _metric_filters(grain: Grain, kind: str, start: Any, end: Any, search: str | None) -> list[Any]:
        model = grain.model
        filters: list[Any] = [model.kind == kind, grain.time >= start, grain.time < end]
        term = search_term(search)
        if term:
            filters.append(contains_text(func.lower(model.name), term))
        return filters

    def screens_page(self, grain: Grain, start: Any, end: Any, *, offset: int, limit: int) -> tuple[list[str], int]:
        """Pantallas de la aplicación web con muestras en el periodo (la de más muestras de Web Vitals primero) y
        cuántas son, en UNA consulta (`count(*) OVER ()`)."""
        model = grain.model
        vitals = func.sum(case((model.kind.in_(VITAL_KINDS), model.count), else_=0))
        stmt = (
            select(model.name, func.count().over().label("total"))
            .where(model.kind.in_(WEB_SCREEN_KINDS), grain.time >= start, grain.time < end)
            .group_by(model.name)
            .order_by(vitals.desc(), model.name)
            .offset(offset)
            .limit(limit)
        )
        rows = list(self.db.execute(stmt).all())
        if rows:
            return [row.name for row in rows], int(rows[0].total)
        return [], self.count_screens(grain, start, end) if offset else 0

    def screen_metrics(self, grain: Grain, screens: Sequence[str], start: Any, end: Any) -> list[Any]:
        """Contadores de cada Web Vital y de las tareas largas de esas pantallas en el periodo."""
        model = grain.model
        stmt = (
            select(model.kind, model.name, *sums(model))
            .where(model.kind.in_(WEB_SCREEN_KINDS), model.name.in_(screens), grain.time >= start, grain.time < end)
            .group_by(model.kind, model.name)
        )
        return list(self.db.execute(stmt).all())

    def count_screens(self, grain: Grain, start: Any, end: Any) -> int:
        model = grain.model
        stmt = select(func.count(func.distinct(model.name))).where(
            model.kind.in_(WEB_SCREEN_KINDS), grain.time >= start, grain.time < end
        )
        return int(self.db.scalar(stmt) or 0)

    # ---------- pg_stat_statements ----------

    def statements_available(self) -> bool:
        """¿La base tiene `pg_stat_statements`? (solo PostgreSQL)."""
        if self.db.get_bind().dialect.name != "postgresql":
            return False
        return bool(self.db.scalar(text("SELECT to_regclass('public.pg_stat_statements') IS NOT NULL")))

    def top_statements(self, sort: str, *, offset: int, limit: int) -> list[Any]:
        """Las consultas que más consumen la base (texto normalizado), por la función SECURITY DEFINER."""
        stmt = text("SELECT * FROM ops.top_statements(:sort, :limit, :offset)")
        return list(self.db.execute(stmt, {"sort": sort, "limit": limit, "offset": offset}).all())

    # ---------- Alertas de peticiones lentas ----------

    def alerts_page(
        self, *, status: str | None, search: str | None, offset: int, limit: int
    ) -> tuple[list[SlowRequestAlert], int]:
        filters = self._alert_filters(status, search)
        stmt = (
            select(SlowRequestAlert)
            .where(*filters)
            .order_by(SlowRequestAlert.last_seen_at.desc(), SlowRequestAlert.id.desc())
            .offset(offset)
            .limit(limit)
        )
        total = self.db.scalar(select(func.count()).select_from(SlowRequestAlert).where(*filters))
        return list(self.db.scalars(stmt).all()), int(total or 0)

    @staticmethod
    def _alert_filters(status: str | None, search: str | None) -> list[Any]:
        filters: list[Any] = [true()]
        if status:
            filters.append(SlowRequestAlert.status == status)
        term = search_term(search)
        if term:
            filters.append(contains_text(func.lower(SlowRequestAlert.route), term))
        return filters

    def alert_counts(self) -> dict[str, int]:
        """{seguimiento: alertas} (el contador del menú y el resumen)."""
        stmt = select(SlowRequestAlert.status, func.count()).group_by(SlowRequestAlert.status)
        return {str(status): int(count) for status, count in self.db.execute(stmt).all()}

    def latest_open(self) -> SlowRequestAlert | None:
        """La alerta ABIERTA que se abrió (o reabrió) más recientemente: el aviso en vivo del ADMIN."""
        stmt = (
            select(SlowRequestAlert)
            .where(SlowRequestAlert.status == SlowAlertStatus.OPEN.value)
            .order_by(SlowRequestAlert.opened_at.desc(), SlowRequestAlert.id.desc())
            .limit(1)
        )
        return self.db.scalars(stmt).first()

    def alert(self, alert_id: int) -> SlowRequestAlert | None:
        return self.db.get(SlowRequestAlert, alert_id)

    def alert_of_route(self, route: str) -> int | None:
        return self.db.scalar(select(SlowRequestAlert.id).where(SlowRequestAlert.route == route))

    def set_alert_status(self, alert_id: int, status: str, *, at: datetime, by: str) -> int:
        """Cambia el seguimiento en UNA sentencia (dice cuántas filas cambió: 0 si no existe). Reabrirla a mano también
        es abrirla: su `opened_at` es el de ahora."""
        values: dict[str, Any] = {"status": status, "status_changed_at": at, "status_changed_by": by}
        if status == SlowAlertStatus.OPEN.value:
            values["opened_at"] = at
        stmt = update(SlowRequestAlert).where(SlowRequestAlert.id == alert_id).values(**values)
        return affected_rows(self.db, stmt)

    def error_report_of_trace(self, trace_id: str, start: datetime, end: datetime) -> int | None:
        """El error registrado con ese traceId en la ventana (la poda de particiones de `occurred_at` acota la
        búsqueda a unos minutos), si lo hay."""
        stmt = (
            select(ErrorOccurrence.report_id)
            .where(
                ErrorOccurrence.occurred_at >= start,
                ErrorOccurrence.occurred_at < end,
                ErrorOccurrence.trace_id == trace_id,
            )
            .order_by(ErrorOccurrence.occurred_at.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def route_totals(self, grain: Grain, route: str, start: Any, end: Any) -> Any:
        """Contadores de una ruta (HTTP) en el periodo."""
        model = grain.model
        stmt = select(*sums(model)).where(
            model.kind == PerfKind.HTTP.value, model.name == route, grain.time >= start, grain.time < end
        )
        return self.db.execute(stmt).one()
