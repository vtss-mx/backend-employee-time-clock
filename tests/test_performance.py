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


def test_assigning_a_shift_to_many_costs_the_same_as_to_few(client, company_headers):
    """Asignar a varios: empleados, asignaciones, sitios y la inserción por lotes (sin una consulta por
    empleado)."""
    from app.core.clock import business_today
    from tests.conftest import create_employee
    from tests.test_shifts import create_shift, create_site

    ids = [
        create_employee(client, company_headers, number=f"EMP-1{i:02d}", email=f"e{i}@empresa.com").json()["data"]["id"]
        for i in range(8)
    ]
    site = create_site(client, company_headers)
    shift = create_shift(client, company_headers)

    def measure(employee_ids: list[int]) -> int:
        body = {
            "shift_id": shift["id"],
            "employee_ids": employee_ids,
            "valid_from": business_today().isoformat(),
            "site_ids": [site["id"]],
        }
        with count_queries() as statements:
            response = client.post("/api/shift-assignments/bulk", json=body, headers=company_headers)
        assert response.status_code == 200 and response.json()["data"]["done"] == len(employee_ids)
        return len(statements)

    assert measure(ids[:2]) == measure(ids[2:])


def test_the_board_with_days_off_does_not_query_once_per_row(client, company_headers):
    """El tablero con festivos, ausencias y días laborables: las mismas consultas para 1 o para 50 filas."""
    from datetime import timedelta

    from app.core.clock import business_today
    from tests.conftest import create_employee
    from tests.test_shifts import assign, create_shift

    shift = create_shift(client, company_headers)
    day = business_today() + timedelta(days=3)
    ids = []
    for i in range(6):
        employee_id = create_employee(
            client, company_headers, number=f"EMP-2{i:02d}", email=f"b{i}@empresa.com"
        ).json()["data"]["id"]
        assign(client, company_headers, employee_id, shift["id"], business_today(), remote=range(7))
        ids.append(employee_id)
    vacation = {"employee_ids": ids[:3], "type": "VACATION", "starts_on": day.isoformat(), "ends_on": day.isoformat()}
    client.post("/api/calendar/absences", json=vacation, headers=company_headers)
    client.post(
        "/api/calendar/workdays", json={"employee_id": ids[0], "work_date": day.isoformat()}, headers=company_headers
    )

    def measure(size: int) -> int:
        url = f"/api/attendance/board?date={day.isoformat()}&size={size}"
        client.get(url, headers=company_headers)
        with count_queries() as statements:
            board = client.get(url, headers=company_headers).json()["data"]
        assert board["day_off"] == 2
        return len(statements)

    assert measure(1) == measure(50)


# ---------------------------------------------------------------- presupuestos de las rutas calientes
#
# Cada ruta caliente tiene su presupuesto (auditoría de rendimiento, migración 0047): un listado cuesta
# lo mismo con 1 que con 50 filas (sin N+1) y TODAS las consultas de la petición (las 2 de la
# autenticación incluidas) caben en un número fijo. Si un cambio necesita más, se justifica y se sube
# aquí a la vista de todos, nunca en silencio. Los presupuestos se miden con SQLite (la suite); en
# PostgreSQL el flush del ORM agrupa inserciones y solo puede costar lo mismo o menos.

#: Consultas por petición de cada ruta (la clave es la ruta sin parámetros de página).
BUDGETS = {
    # Asistencia y calendario (empresa)
    "/api/attendance/board": 12,
    "/api/attendance/sessions": 6,
    "/api/calendar/absences": 5,
    "/api/calendar/workdays": 5,
    "/api/calendar/holidays": 4,
    # Empleado
    "/api/me/attendance/today": 12,
    "/api/me/attendance/history": 5,
    # Operaciones (cuestan lo mismo para 1 que para N empleados o descansos)
    "POST /api/attendance/sessions": 16,
    "POST /api/calendar/absences": 5,
    # Catálogos de la empresa y bandejas
    "/api/employees": 6,
    "/api/shifts": 5,
    "/api/sites": 5,
    "/api/departments": 6,
    "/api/validators": 7,
    "/api/shift-requests": 8,
    "/api/employees/{id}/verifications": 5,
    # API de integración
    "/api/integrations/v1/attendance": 7,
    "/api/integrations/v1/attendance/feed": 4,
    # ADMIN de la plataforma
    "/api/admin/companies": 6,
    "/api/admin/errors": 5,
    "/api/admin/face-security": 6,
}


def page_cost(client, url: str, headers: dict, size: int) -> tuple[int, int]:
    """(consultas, filas) de una página de `size` (la primera llamada calienta catálogos y política)."""
    full = f"{url}{'&' if '?' in url else '?'}size={size}"
    client.get(full, headers=headers)
    with count_queries() as statements:
        response = client.get(full, headers=headers)
    assert response.status_code == 200, response.text
    return len(statements), len(response.json()["data"]["items"])


def assert_flat(client, url: str, headers: dict, budget: str) -> None:
    """La página de 50 cuesta lo mismo que la de 1 (y de verdad trae más de una fila) y cabe en su
    presupuesto."""
    one, _ = page_cost(client, url, headers, 1)
    many, rows = page_cost(client, url, headers, 50)
    assert rows > 1, f"{url}: siembra más de una fila para medir el N+1"
    assert many == one, (url, one, many)
    assert one <= BUDGETS[budget], (url, one, BUDGETS[budget])


def cost(client, method: str, url: str, headers: dict, **kwargs) -> int:
    """Consultas de una petición (que debe salir bien)."""
    with count_queries() as statements:
        response = client.request(method, url, headers=headers, **kwargs)
    assert response.status_code < 300, response.text
    return len(statements)


def _seed_logs(employee_id: int, rows: int) -> None:
    """Bitácora escrita directo en la BD (solo se miden sus lecturas)."""
    from app.core.database import SessionLocal
    from app.models import Employee, VerificationLog, VerificationMethod

    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        assert employee is not None
        db.add_all(
            VerificationLog(
                employee_id=employee.id,
                user_id=employee.user_id,
                company_id=employee.company_id,
                method=VerificationMethod.FACE,
                success=i % 2 == 0,
                reason=None if i % 2 == 0 else "NO_MATCH",
            )
            for i in range(rows)
        )
        db.commit()


def test_attendance_and_calendar_routes_fit_their_budget(client, company_headers, monkeypatch):
    """Tablero, historial, "hoy" del empleado, registro manual, vacaciones colectivas y los listados del
    calendario: presupuesto fijo y sin N+1."""
    from datetime import timedelta

    from app.core.clock import business_today
    from tests.conftest import create_employee
    from tests.test_attendance import server_clock
    from tests.test_attendance_calendar import remote_worker
    from tests.test_shifts import assign

    clock = server_clock(monkeypatch)
    ana = remote_worker(client, company_headers)
    day = ana["day"]
    crew = [ana["id"]]
    for i in range(4):
        created = create_employee(client, company_headers, number=f"EMP-3{i:02d}", email=f"c{i}@empresa.com")
        crew.append(created.json()["data"]["id"])
        assign(client, company_headers, crew[-1], ana["shift"]["id"], business_today(), remote=range(7))

    # Registrar la jornada de quien no checó: un número fijo de consultas para cualquier empleado (los
    # descansos y los registros de la bitácora van en una inserción cada uno).
    clock(day + timedelta(days=3), "18:00")
    spent = set()
    for offset, employee_id in enumerate([ana["id"], ana["id"], *crew]):
        body = {
            "employee_id": employee_id,
            "work_date": (day + timedelta(days=offset % 3)).isoformat(),
            "check_in": "08:05",
            "check_out": "16:00",
            "breaks": [{"start": "12:00", "end": "12:30"}],
            "reason": "Olvidó checar",
        }
        spent.add(cost(client, "POST", "/api/attendance/sessions", company_headers, json=body))
    assert spent == {BUDGETS["POST /api/attendance/sessions"]}, spent

    # Vacaciones colectivas: lo mismo para 2 que para 3 empleados.
    def vacation(ids: list[int]) -> int:
        start = day + timedelta(days=20)
        body = {"employee_ids": ids, "type": "VACATION", "starts_on": start.isoformat(), "ends_on": start.isoformat()}
        return cost(client, "POST", "/api/calendar/absences", company_headers, json=body)

    assert vacation(crew[:2]) == vacation(crew[2:]) == BUDGETS["POST /api/calendar/absences"]
    for employee_id in crew[:3]:
        workday = {"employee_id": employee_id, "work_date": (day + timedelta(days=20)).isoformat()}
        assert client.post("/api/calendar/workdays", json=workday, headers=company_headers).status_code == 201
    year = (day + timedelta(days=20)).year
    official = client.post("/api/calendar/holidays/official", params={"year": year}, headers=company_headers)
    assert official.status_code == 200

    period = f"start={day.isoformat()}&end={(day + timedelta(days=3)).isoformat()}"
    for url, budget in (
        (f"/api/attendance/board?date={day.isoformat()}", "/api/attendance/board"),
        ("/api/attendance/sessions", "/api/attendance/sessions"),
        (f"/api/attendance/sessions?{period}", "/api/attendance/sessions"),
        ("/api/attendance/sessions?status=CLOSED", "/api/attendance/sessions"),
        ("/api/calendar/absences", "/api/calendar/absences"),
        ("/api/calendar/absences?status=APPROVED", "/api/calendar/absences"),
        (f"/api/calendar/absences?start={(day + timedelta(days=19)).isoformat()}", "/api/calendar/absences"),
        ("/api/calendar/workdays", "/api/calendar/workdays"),
        (f"/api/calendar/holidays?year={year}", "/api/calendar/holidays"),
    ):
        assert_flat(client, url, company_headers, budget)
    assert_flat(client, "/api/me/attendance/history", ana["headers"], "/api/me/attendance/history")
    today = cost(client, "GET", "/api/me/attendance/today", ana["headers"])
    assert today <= BUDGETS["/api/me/attendance/today"], today


def test_company_admin_and_integration_lists_fit_their_budget(client, company_headers, admin_headers):
    """Catálogos de la empresa, bandejas, la API de integración (página y cursor) y las pantallas del
    ADMIN: sin N+1 y dentro de su presupuesto."""
    from datetime import UTC, datetime, timedelta

    from app.core.clock import business_today
    from app.core.database import SessionLocal
    from app.models import Employee, ErrorReport, ShiftChangeRequest
    from tests.conftest import create_company, create_employee
    from tests.test_api_keys import new_key
    from tests.test_departments import new_department
    from tests.test_shifts import assign, create_shift, create_site
    from tests.test_validators import create_validator

    shift = create_shift(client, company_headers)
    night = create_shift(client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00")
    sites = [create_site(client, company_headers, name=f"Planta {i}")["id"] for i in range(3)]
    crew = []
    for i in range(4):
        created = create_employee(client, company_headers, number=f"EMP-4{i:02d}", email=f"d{i}@empresa.com")
        crew.append(created.json()["data"]["id"])
        assign(client, company_headers, crew[-1], shift["id"], business_today(), sites=sites)
    for name in ("Producción", "Almacén", "Ventas"):
        new_department(client, company_headers, name=name)
    for i in range(3):
        assert create_validator(client, company_headers, name=f"Recepción {i}", email=f"v{i}@empresa.com").is_success
    assert create_company(client, admin_headers).status_code == 201
    with SessionLocal() as db:
        first = db.get(Employee, crew[0])
        assert first is not None
        now = datetime.now(UTC)
        db.add_all(
            ShiftChangeRequest(
                company_id=first.company_id,
                employee_id=employee_id,
                shift_id=night["id"],
                valid_from=business_today() + timedelta(days=5),
                reason="Estudio",
            )
            for employee_id in crew
        )
        db.add_all(
            ErrorReport(
                fingerprint=f"f{i}",
                source="HTTP",
                severity="ERROR",
                code="DATABASE_TIMEOUT",
                message="La base tardó",
                first_seen_at=now,
                last_seen_at=now,
                status_changed_by_id=first.user_id,
            )
            for i in range(3)
        )
        db.commit()
    _seed_logs(crew[0], 6)

    for url in ("/api/employees", "/api/shifts", "/api/sites", "/api/departments", "/api/validators"):
        assert_flat(client, url, company_headers, url)
    for url in ("/api/shift-requests", "/api/shift-requests?status=PENDING"):
        assert_flat(client, url, company_headers, "/api/shift-requests")
    verifications = f"/api/employees/{crew[0]}/verifications"
    assert_flat(client, verifications, company_headers, "/api/employees/{id}/verifications")
    integration = {"X-API-Key": new_key(client, company_headers)["secret"]}
    for url in ("/api/integrations/v1/attendance", "/api/integrations/v1/attendance?since=2020-01-01T00:00:00Z"):
        assert_flat(client, url, integration, "/api/integrations/v1/attendance")
    feed = cost(client, "GET", "/api/integrations/v1/attendance/feed?limit=500", integration)
    assert feed <= BUDGETS["/api/integrations/v1/attendance/feed"], feed
    for url in ("/api/admin/companies", "/api/admin/errors"):
        assert_flat(client, url, admin_headers, url)
    overview = cost(client, "GET", "/api/admin/face-security", admin_headers)
    assert overview <= BUDGETS["/api/admin/face-security"], overview
