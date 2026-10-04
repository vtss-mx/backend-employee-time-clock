"""El catálogo del asistente crece con el sistema y no deja datos fuera (ni expone lo que no debe)."""

from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  (registra todas las tablas)
from app.core.database import Base, SessionLocal
from app.repositories.report_repository import SOURCES, ReportQuery, ReportRepository, Window, _Sql
from app.services.catalog_service import get_catalogs
from app.services.reporting.datasets import DATASETS, EXCLUDED_TABLES


def _tenant_tables() -> set[str]:
    return {f"{t.schema}.{t.name}" for t in Base.metadata.tables.values() if "company_id" in t.c}


def test_every_company_table_is_reported_or_excluded_with_its_reason():
    """Un módulo nuevo con datos de la empresa debe sumarse al asistente (o decir por qué no)."""
    covered = {source for dataset in DATASETS for source in dataset.sources}
    assert not _tenant_tables() - covered - set(EXCLUDED_TABLES)
    assert set(EXCLUDED_TABLES) <= _tenant_tables()  # la lista de exclusiones no tiene tablas inventadas
    assert all(reason for reason in EXCLUDED_TABLES.values())


def test_every_dataset_has_its_query_and_every_column_its_expression():
    assert set(SOURCES) == {dataset.code for dataset in DATASETS}
    with SessionLocal() as db:
        repo = ReportRepository(db, company_id=1)
        for dataset in DATASETS:
            source = repo.source(dataset.code, Window())
            assert set(source.columns) == {c.code for c in dataset.columns}, dataset.code
            assert dataset.sort in source.columns and (dataset.time is None or dataset.time in source.columns)


def test_catalog_invariants():
    catalogs = get_catalogs()
    for dataset in DATASETS:
        assert dataset.words and dataset.examples and dataset.default_columns
        for column in dataset.columns:
            if column.kind == "bool":  # cada sí/no dice cómo se nombran sus dos valores
                assert {v.value for v in column.values} == {True, False}, column.code
            if column.kind == "category":  # cada categoría sale de un catálogo de la BD
                assert column.catalog and column.catalog in catalogs.entries, column.code
            assert not column.metric or column.kind == "number", column.code


def test_postgresql_expressions_are_built_for_production():
    """En producción (PostgreSQL) las fechas se calculan en la hora del negocio con sus funciones."""
    with SessionLocal() as db:
        sql = _Sql(db)
        sql.postgres = True
        moment = Base.metadata.tables["attendance.verification_logs"].c.created_at
        compiled = [
            str(expression.compile(dialect=postgresql.dialect()))
            for expression in (
                sql.day(moment),
                sql.week(moment),
                sql.month(moment),
                sql.hour(moment),
                sql.decimal_hour(moment),
                sql.hours_between(moment, moment),
                sql.join_text(moment),
            )
        ]
    assert "timezone(" in compiled[0] and "date_trunc" in compiled[1] and "to_char" in compiled[2]
    assert "EXTRACT(epoch" in compiled[5] and "string_agg" in compiled[6]


def test_rows_without_an_explicit_order_follow_the_main_key(client, company_headers):
    with SessionLocal() as db:
        repo = ReportRepository(db, company_id=1)
        assert repo.rows(ReportQuery("employees", columns=("full_name",)), offset=0, limit=5) == []
