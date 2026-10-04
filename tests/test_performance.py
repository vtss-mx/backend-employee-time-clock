"""Presupuesto de consultas de las rutas más usadas: si crece, la prueba lo detecta."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event

from app.core.database import engine
from tests.conftest import approved_employee
from tests.test_validators import validator_headers


@contextmanager
def count_queries() -> Iterator[list[str]]:
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        if not statement.lstrip().upper().startswith(("PRAGMA", "SAVEPOINT", "RELEASE")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_authenticating_a_request_costs_two_queries_for_every_role(client, company_headers):
    employee = approved_employee(client, company_headers)
    validator = validator_headers(client, company_headers, mode="QR")
    for headers in (company_headers, employee, validator):
        client.get("/api/users/me", headers=headers)  # calienta catálogos y política
        with count_queries() as statements:
            assert client.get("/api/users/me", headers=headers).status_code == 200
        assert len(statements) <= 2, statements


def test_lists_do_not_query_once_per_row(client, company_headers):
    """Empleados y validaciones: el número de consultas no crece con el tamaño de la página."""
    from tests.conftest import create_employee, login, submit_enrollment

    def measure(url: str) -> int:
        client.get(url, headers=company_headers)
        with count_queries() as statements:
            assert client.get(url, headers=company_headers).status_code == 200
        return len(statements)

    def add_person(i: int) -> None:
        email = f"p{i}@empresa.com"
        create_employee(client, company_headers, number=f"EMP-00{i}", email=email, phone=f"+52 662 100 000{i}")
        submit_enrollment(client, login(client, email, "Empleado123"))

    for i in range(1, 3):
        add_person(i)
    few = {url: measure(url) for url in ("/api/employees?size=1", "/api/enrollments?size=1")}
    for i in range(3, 7):
        add_person(i)
    for url, queries in few.items():
        assert measure(url.replace("size=1", "size=50")) == queries, url
