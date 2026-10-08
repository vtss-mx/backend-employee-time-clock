"""Presupuesto de consultas de las rutas más usadas: si crece, la prueba lo detecta."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event

from app.core.database import engine
from tests.conftest import approved_employee
from tests.test_attendance import worker  # noqa: F401 (fixture)
from tests.test_validators import validator_headers


@contextmanager
def count_queries() -> Iterator[list[str]]:
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        # No son consultas de la ruta: lo propio del motor y, en PostgreSQL, la declaración del alcance de la
        # seguridad por fila al empezar cada transacción (`row_security`: una por transacción, README "Seguridad
        # por fila", con su costo medido aparte).
        if statement.lstrip().upper().startswith(("PRAGMA", "SAVEPOINT", "RELEASE")) or "set_config(" in statement:
            return
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
        # En inglés cuesta lo mismo: los catálogos de cada idioma ya están en memoria (regla 16, sin consultas).
        with count_queries() as statements:
            english = client.get("/api/users/me", headers={**headers, "Accept-Language": "en-US"})
        assert english.status_code == 200 and english.headers["Content-Language"] == "en-US"
        assert len(statements) <= 2, statements


def test_catalogs_from_the_shared_cache_cost_no_catalog_queries(client, company_headers):
    """Con Redis, una réplica cuya copia local venció recarga los catálogos con UNA lectura de Redis y cero consultas
    SQL (Redis no cuenta como consulta); la petición queda en el presupuesto de la autenticación."""
    from app.core.cache import use_cache
    from app.services import catalog_service
    from tests.redis_support import FakeRedis, shared

    server = FakeRedis()
    use_cache(shared(server))
    try:
        catalog_service.clear_catalog_cache()
        assert client.get("/api/catalogs", headers=company_headers).status_code == 200  # la base → Redis
        with count_queries() as statements:
            assert client.get("/api/catalogs", headers=company_headers).status_code == 200
        budget = len(statements)
        assert budget <= 2, statements
        catalog_service._cache.clear()  # otra réplica (o esta, vencida): Redis responde, la base no se toca
        server.calls.clear()
        with count_queries() as statements:
            assert client.get("/api/catalogs", headers=company_headers).status_code == 200
        assert len(statements) == budget and server.calls == ["get"]
    finally:
        use_cache(None)


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
    """Asignar a varios: empleados, asignaciones y la inserción por lotes (sin una consulta por
    empleado)."""
    from app.core.clock import business_today
    from tests.conftest import create_employee
    from tests.test_shifts import create_shift, create_site

    ids = [
        create_employee(client, company_headers, number=f"EMP-1{i:02d}", email=f"e{i}@empresa.com").json()["data"]["id"]
        for i in range(8)
    ]
    site = create_site(client, company_headers)
    shift = create_shift(client, company_headers, sites=[site["id"]])

    def measure(employee_ids: list[int]) -> int:
        body = {"shift_id": shift["id"], "employee_ids": employee_ids, "valid_from": business_today().isoformat()}
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
        assign(client, company_headers, employee_id, shift["id"], business_today())
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
    # +1 (foto de perfil, decisión del dueño 2026-10-06): la versión de la foto de las cuentas de la página, en UNA
    # consulta por la llave primaria (la página no carga las cuentas a propósito: ver `ShiftRepository.assigned_on`).
    "/api/attendance/board": 13,
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
    # +1 (migración 0048): los sitios de los turnos de la página, en una consulta (el turno dice dónde se checa).
    "/api/shifts": 6,
    # +1 (migración 0070): los kioscos vigentes de los sitios de la página, en un GROUP BY.
    "/api/sites": 6,
    "/api/departments": 6,
    "/api/validators": 8,  # + los activos de su límite ("N de M", una cuenta por el índice de cuentas)
    # +1 (migración 0048): los sitios del turno pedido de cada solicitud, en una consulta para la página.
    "/api/shift-requests": 9,
    "/api/employees/{id}/verifications": 5,
    # API de integración
    "/api/integrations/v1/attendance": 7,
    "/api/integrations/v1/attendance/feed": 4,
    # ADMIN de la plataforma
    "/api/admin/companies": 6,
    "/api/admin/errors": 5,
    # Personas con su foto de perfil (decisión del dueño, 2026-10-06): la versión viaja con la cuenta que la página ya
    # carga (empleados con su cuenta por JOIN; administradores: la cuenta misma; quién provocó cada error: la lectura de
    # sus cuentas que ya se hacía): las mismas consultas que antes de la foto.
    "/api/admin/companies/{id}/employees": 6,
    "/api/admin/companies/{id}/admins": 6,
    "/api/admin/errors/{id}/occurrences": 7,
    # +1 (migración 0062): el cociente rostro/fondo del destello medido (otra lectura acotada de las métricas); +1
    # (0066): el protocolo de captura (destello dictado, ráfaga y pulso) en UNA lectura acotada.
    "/api/admin/face-security": 8,
    # Cobranza y consumo (ADMIN): saldos y consumo de toda la página en consultas agrupadas
    "/api/admin/billing/companies": 7,
    "/api/admin/billing/companies/{id}/charges": 5,
    "/api/admin/billing/companies/{id}/payments": 5,
    "/api/admin/billing/companies/{id}/statement": 5,
    "/api/admin/billing/overview": 3,
    "/api/admin/billing/companies/{id}": 6,
    "/api/admin/usage/companies": 6,
    "/api/admin/usage/companies/{id}/users": 8,
    "/api/admin/usage/companies/{id}/routes": 5,
    "/api/admin/usage/overview": 7,
    "/api/admin/usage/companies/{id}": 11,
    # Imágenes en el bucket: el detalle del registro facial no carga la foto de la BD (su referencia viene en
    # la misma fila) y el estado del ADMIN son conteos fijos (guardadas de cada tipo, cola de borrados y su
    # última vuelta). +1 (0079): las respuestas de la verificación por voz del registro (una consulta, acotada a
    # las preguntas de la sesión).
    "GET /api/enrollments/{id}": 6,
    # Los conteos con tope de TODOS los tipos de imagen del bucket (fotos del registro y su foto inicial, comprobantes,
    # fotos de perfil, evidencia, documentos y videos) van en UNA consulta con un subconteo por tipo (antes, una por
    # tipo: 10 con seis tipos); un tipo nuevo en `STORED_IMAGES` ya no agrega consultas (0083).
    "/api/admin/errors/server": 5,
    # Foto de perfil (migración 0061; la imagen vive en el bucket): leerla es la autenticación + UNA consulta (quién
    # puede verla y su referencia, juntas); subirla o quitarla, una transacción corta de sentencias fijas (bloquear la
    # cuenta, encolar la foto anterior para el bucket, borrar su referencia, guardar la nueva y la versión).
    "GET /api/users/{id}/avatar": 3,
    "PUT /api/users/me/avatar": 7,
    "DELETE /api/users/me/avatar": 6,
    # Rendimiento (migración 0063): cada lectura es un número fijo de consultas agrupadas sobre un periodo acotado
    # (los percentiles salen de sumar histogramas en la base); medir una petición no agrega ninguna (0 extra) y un
    # lote del navegador tampoco (solo su límite por sesión o IP).
    "/api/admin/performance/overview": 6,
    "/api/admin/performance/metrics": 3,
    "/api/admin/performance/metrics/series": 4,
    "/api/admin/performance/web-vitals": 4,
    # SQLite: ninguna; PostgreSQL: ¿existe pg_stat_statements? y, si existe, la función que la lee.
    "/api/admin/performance/statements": 4,
    "/api/admin/performance/alerts": 4,
    "/api/admin/performance/alerts/summary": 4,
    "GET /api/admin/performance/alerts/{id}": 5,
    "PATCH /api/admin/performance/alerts/{id}": 7,
    # Antifraude (migración 0062): la bandeja de casos es su conteo y su página más empresas, empleados y cuentas de
    # la página en tres consultas (sin una por caso); el detalle suma sus intentos, su historial y su evidencia (una
    # consulta cada uno); el historial de la política, la empresa (404), su conteo, su página y sus cuentas; los
    # contadores del menú, un conteo con tope.
    "/api/admin/fraud-cases": 7,
    # Deriva de señales (antifraude fase 3): cada lectura filtra una ventana (la pedida o la más reciente) y la lista
    # de empresas no une con `companies` (el nombre va copiado en la fila).
    "/api/admin/drift": 5,
    "/api/admin/drift/companies": 5,
    "/api/admin/drift/summary": 8,
    # Llaves de acceso: las de la cuenta (una página) y entrar con una (la llave con su cuenta y sus empleos, el reto
    # usado, el contador y la sesión: lo mismo que la contraseña).
    "/api/auth/passkeys": 4,
    "POST /api/auth/login/passkey": 14,
    "/api/admin/fraud-cases/count": 3,
    "GET /api/admin/fraud-cases/{id}": 9,
    "/api/admin/companies/{id}/verification-policy/changes": 5,
    "/api/attendance/reviews/count": 3,
    # Registrar con el rostro (antifraude 1b, migración 0065): +2 por el dispositivo del empleado (su fila y quién más
    # de la empresa usó su llave, en UNA lectura; su uso, en una inserción atómica). La red de la IP (base local en
    # memoria), la telemetría y las tablas JPEG no consultan nada, y el 1:N en cada 1:1 usa la galería en memoria (una
    # consulta más, su huella, cada RISK_IDENTITY_GALLERY_MAX_AGE_SECONDS). El registro de asistencia reutiliza la
    # lectura del registro anterior que ya hacía el viaje imposible (LOCATION_JUMP y NETWORK_JUMP salen de ella).
    "POST /api/verification/face": 16,
    # API pública de verificación (SDK móviles, migración 0084). La llave cuesta 3 consultas (su hash, sus permisos y su
    # empresa; su último uso, a lo más una vez por minuto) en lugar de las 2 de la sesión; el empleado se lee por su id
    # o su número (1) y no hay dispositivo del empleado (−2: la prueba del dispositivo es una firma, sin consultas). El
    # reto: la política (en caché), si la empresa está bajo ataque (1) y reemplazar el reto del dispositivo (2).
    "POST /api/integrations/v1/verification/challenge": 7,
    "POST /api/integrations/v1/verification/verify": 16,
    # El 1:N cuesta 2 más que el 1:1 (medido, no estimado): el 1:1 lee al empleado y sus muestras ANTES del análisis;
    # el 1:N las lee DESPUÉS, del ganador que encontró la galería en memoria (su fila para la respuesta y sus muestras
    # para el consenso de la ráfaga), y además revisa que la galería siga fresca (empleados activos y cuenta de
    # muestras: 2). Todo es un número fijo: no depende de cuántos empleados tenga la empresa.
    "POST /api/integrations/v1/verification/identify": 18,
    "POST /api/me/attendance/{action}": 28,
    # Registro facial con muchas fotos (decisión del dueño, 2026-10-06): las mismas consultas con 3 que con 36 fotos.
    # Las referencias se guardan en UNA inserción (`add_all`) y las huellas de todas las fotos útiles en UNA sentencia
    # atómica (`CaptureFingerprintRepository.claim`); elegirlas es solo CPU. +5 (0079, verificación por voz): borrar
    # un registro anterior sin terminar (1) y las preguntas elegibles (el nombre de la empresa, el departamento, la
    # asignación de turno vigente y los sitios del turno: una consulta cada uno), sin importar cuántas fotos. +1 (0083,
    # los tres pasos independientes): leer la foto inicial ANTES del reto (sin ella, 409 sin gastar el reto ni segundos
    # de CPU). Tomarla (`DELETE … RETURNING`, atómico) reemplaza al UPDATE que anotaba la foto subida: su referencia va
    # en la misma inserción del registro y no se vuelve a subir.
    "POST /api/enrollment/face": 19,
    # Los tres pasos independientes del registro (decisión del dueño, 2026-10-07; migración 0083). La foto inicial: la
    # política, la cola del bucket del borrador anterior, su borrado y la inserción del nuevo con su referencia. El
    # avance: el borrador (o el registro pendiente con sus respuestas y, para decir «2 de 3», las preguntas elegibles:
    # el nombre de la empresa y la asignación de turno vigente). La sesión de voz: el registro pendiente, sus respuestas
    # aceptadas y las mismas preguntas elegibles.
    "POST /api/enrollment/photo": 6,
    "GET /api/enrollment/progress": 7,
    "POST /api/enrollment/voice/start": 7,
    # Borrado lógico (migración 0068): la papelera de cada listado cuesta lo mismo que el listado (la misma página por
    # el índice parcial `ix_*_deleted`, sin consultas de más: la condición de lo vigente viaja en la sentencia), y
    # restaurar es el registro con su candado, la revisión de cada dato único que puede chocar y su lectura.
    "/api/employees?deleted=true": 6,
    "/api/validators?deleted=true": 8,
    "/api/departments?deleted=true": 6,
    "/api/sites?deleted=true": 6,
    "/api/shifts?deleted=true": 6,
    "/api/admin/companies?deleted=true": 6,
    "/api/calendar/holidays?deleted=true": 4,
    "/api/calendar/workdays?deleted=true": 5,
    "/api/employees/{id}/shift-assignments?deleted=true": 7,
    # Empleado: el candado, el límite del plan, número y tres documentos, correo y teléfono de su cuenta, guardar (él,
    # su cuenta y el evento del cobro) y su lectura (la del detalle). Una empresa: su candado, su RFC, cada cuenta que
    # se fue con ella, sus validadores y su evento del cobro, y su detalle.
    "POST /api/employees/{id}/restore": 18,
    "POST /api/validators/{id}/restore": 13,
    "POST /api/departments/{id}/restore": 8,
    "POST /api/sites/{id}/restore": 7,
    "POST /api/shifts/{id}/restore": 8,
    "POST /api/calendar/holidays/{id}/restore": 5,
    "POST /api/calendar/workdays/{id}/restore": 10,
    "POST /api/admin/companies/{id}/restore": 14,
    # Identificador fiscal (migración 0074): dar de alta (su revisión, el correo, la empresa, su política, su
    # administrador y la lectura con sus conteos) y editarlo cuestan UNA consulta más que antes de capturarlo (la
    # revisión por el único parcial `uq_companies_tax_id`, como la del RFC); la validación en vivo, la autenticación y
    # esa misma revisión (el tipo y su regla vienen de los catálogos en memoria).
    "POST /api/admin/companies": 10,
    "PUT /api/admin/companies/{id}": 8,
    "/api/validation?field=company_tax_id": 3,
    # Antifraude 2b (migración 0070): los kioscos de un sitio (el sitio, su conteo y su página; la papelera igual) y el
    # código que pide la tableta (su límite por IP y UNA lectura del kiosco con su sitio y su empresa; la última vez
    # que se vio se anota cada SITE_KIOSK_SEEN_SECONDS). Firmar cada identificación del validador y revisar su
    # ubicación no agregan consultas (la sesión ya está en memoria; la distancia es aritmética).
    "/api/sites/{id}/kiosks": 5,
    "/api/sites/{id}/kiosks?deleted=true": 5,
    "POST /api/kiosk/code": 3,
    # Documentos de la empresa (migración 0075): el listado y su papelera son su conteo y su página por sus índices; el
    # ADMIN suma la empresa (404 si no existe). Subir es UNA inserción (el archivo va al bucket sin transacción
    # abierta: ninguna consulta espera a la red); descargar, el documento (el bucket después de cerrar la lectura);
    # eliminar y restaurar, el documento bloqueado y su cambio.
    "/api/documents": 4,
    "/api/documents?deleted=true": 4,
    "/api/admin/companies/{id}/documents": 5,
    "POST /api/documents": 3,
    "POST /api/admin/companies/{id}/documents": 4,
    "GET /api/documents/{id}/file": 3,
    "GET /api/admin/companies/{id}/documents/{id}/file": 4,
    "DELETE /api/documents/{id}": 4,
    "POST /api/documents/{id}/restore": 4,
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
        assign(client, company_headers, crew[-1], ana["shift"]["id"], business_today())

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

    # "En revisión" (motor de riesgo, migración 0062): el filtro va por su índice parcial y cuesta lo mismo.
    from sqlalchemy import update

    from app.core.database import SessionLocal
    from app.models import WorkSession

    with SessionLocal() as db:
        db.execute(update(WorkSession).values(review_status="PENDING", review_reasons="CAPTURE"))
        db.commit()
    assert_flat(client, "/api/attendance/sessions?in_review=true", company_headers, "/api/attendance/sessions")
    pending = cost(client, "GET", "/api/attendance/reviews/count", company_headers)
    assert pending <= BUDGETS["/api/attendance/reviews/count"], pending


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

    # Varios turnos con varios sitios cada uno: la lista de turnos y las solicitudes leen los sitios por lotes.
    sites = [create_site(client, company_headers, name=f"Planta {i}")["id"] for i in range(3)]
    shift = create_shift(client, company_headers, sites=sites)
    night = create_shift(
        client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00", sites=sites[:2]
    )
    create_shift(client, company_headers, name="Vespertino", start_time="14:00", end_time="22:00", sites=sites[1:])
    crew = []
    for i in range(4):
        created = create_employee(client, company_headers, number=f"EMP-4{i:02d}", email=f"d{i}@empresa.com")
        crew.append(created.json()["data"]["id"])
        assign(client, company_headers, crew[-1], shift["id"], business_today())
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

    # Personas con su foto de perfil (decisión del dueño, 2026-10-06): los listados del ADMIN que muestran gente la
    # traen de la cuenta que ya cargan (o en la misma consulta), nunca con una consulta por fila.
    from sqlalchemy import select

    from app.models import ErrorOccurrence

    with SessionLocal() as db:
        company_id = db.scalar(select(Employee.company_id).where(Employee.id == crew[0]))
        report_id = db.scalar(select(ErrorReport.id).order_by(ErrorReport.id))
        users = list(db.scalars(select(Employee.user_id).where(Employee.id.in_(crew))))
        db.add_all(
            ErrorOccurrence(report_id=report_id, user_id=user_id, company_id=company_id, message="La base tardó")
            for user_id in users
        )
        db.commit()
    admin = {"admin_email": "segundo@empresa.com", "admin_password": "Empresa1234"}
    assert client.post(f"/api/admin/companies/{company_id}/admins", json=admin, headers=admin_headers).is_success
    for url, budget in (
        (f"/api/admin/companies/{company_id}/employees", "/api/admin/companies/{id}/employees"),
        (f"/api/admin/companies/{company_id}/admins", "/api/admin/companies/{id}/admins"),
        (f"/api/admin/errors/{report_id}/occurrences", "/api/admin/errors/{id}/occurrences"),
    ):
        assert_flat(client, url, admin_headers, budget)


def test_the_trash_and_restoring_fit_their_budget(client, company_headers, admin_headers):
    """«Eliminados» (borrado lógico): la papelera de cada listado sin N+1 y en el presupuesto de su listado; restaurar
    en un número fijo de consultas."""
    from datetime import date, timedelta

    from app.core.clock import business_today
    from tests.conftest import create_company, create_employee
    from tests.test_departments import new_department
    from tests.test_shifts import assign, create_shift, create_site
    from tests.test_validators import create_validator

    day = date(business_today().year + 1, 6, 1)  # tres días del mismo año, siempre por venir
    deleted: dict[str, list[str]] = {}

    def gone(url: str) -> None:
        assert client.delete(url, headers=company_headers).status_code == 200, url
        deleted.setdefault(url.rsplit("/", 1)[0], []).append(url)

    crew = [
        create_employee(client, company_headers, number=f"EMP-7{i}", email=f"t{i}@empresa.com").json()["data"]["id"]
        for i in range(4)
    ]
    keep, *others = crew
    current, night = create_shift(client, company_headers), create_shift(client, company_headers, name="Nocturno")
    assign(client, company_headers, keep, current["id"], business_today())
    for i in range(3):
        later = business_today() + timedelta(days=3 + i)
        change = assign(client, company_headers, keep, night["id"], later).json()["data"]
        gone(f"/api/shift-assignments/{change['id']}")
        holiday = {"holiday_date": (day + timedelta(days=i)).isoformat(), "name": f"F{i}"}
        assert client.post("/api/calendar/holidays", json=holiday, headers=company_headers).status_code == 201
    holidays = client.get("/api/calendar/holidays", params={"year": day.year}, headers=company_headers)
    for holiday in holidays.json()["data"]["items"]:
        body = {"employee_id": keep, "work_date": holiday["holiday_date"]}
        workday = client.post("/api/calendar/workdays", json=body, headers=company_headers).json()["data"]
        gone(f"/api/calendar/workdays/{workday['id']}")
        gone(f"/api/calendar/holidays/{holiday['id']}")
    for i in range(3):
        gone(f"/api/departments/{new_department(client, company_headers, name=f'Área {i}')['id']}")
        gone(f"/api/sites/{create_site(client, company_headers, name=f'Planta {i}')['id']}")
        gone(f"/api/shifts/{create_shift(client, company_headers, name=f'Turno {i}')['id']}")
        assert create_validator(client, company_headers, name=f"Caseta {i}", email=f"c{i}@empresa.com").is_success
    for validator in client.get("/api/validators", headers=company_headers).json()["data"]["items"]:
        gone(f"/api/validators/{validator['id']}")
    for employee_id in others:
        gone(f"/api/employees/{employee_id}")
    for i, rfc in enumerate(("TRA120315AB1", "TRB120315AB1")):
        created = create_company(client, admin_headers, rfc=rfc, admin_email=f"a{i}@t.com").json()
        assert client.delete(f"/api/admin/companies/{created['data']['id']}", headers=admin_headers).status_code == 200

    for url in ("/api/employees", "/api/shifts", "/api/sites", "/api/departments", "/api/validators"):
        assert_flat(client, f"{url}?deleted=true", company_headers, f"{url}?deleted=true")
    assert_flat(client, "/api/calendar/workdays?deleted=true", company_headers, "/api/calendar/workdays?deleted=true")
    holidays_url = f"/api/calendar/holidays?deleted=true&year={day.year}"
    assert_flat(client, holidays_url, company_headers, "/api/calendar/holidays?deleted=true")
    assignments = f"/api/employees/{keep}/shift-assignments?deleted=true"
    assert_flat(client, assignments, company_headers, "/api/employees/{id}/shift-assignments?deleted=true")
    assert_flat(client, "/api/admin/companies?deleted=true", admin_headers, "/api/admin/companies?deleted=true")

    for prefix, budget in (
        ("/api/employees", "POST /api/employees/{id}/restore"),
        ("/api/validators", "POST /api/validators/{id}/restore"),
        ("/api/departments", "POST /api/departments/{id}/restore"),
        ("/api/sites", "POST /api/sites/{id}/restore"),
        ("/api/shifts", "POST /api/shifts/{id}/restore"),
        ("/api/calendar/holidays", "POST /api/calendar/holidays/{id}/restore"),
        ("/api/calendar/workdays", "POST /api/calendar/workdays/{id}/restore"),
    ):
        spent = cost(client, "POST", f"{deleted[prefix][-1]}/restore", company_headers)
        assert spent <= BUDGETS[budget], (prefix, spent)
    company = client.get("/api/admin/companies?deleted=true", headers=admin_headers).json()["data"]["items"][0]
    restored = cost(client, "POST", f"/api/admin/companies/{company['id']}/restore", admin_headers)
    assert restored <= BUDGETS["POST /api/admin/companies/{id}/restore"], restored


def test_billing_and_usage_screens_fit_their_budget(client, admin_headers, company_headers, monkeypatch):
    """Cobranza y consumo del ADMIN: listados sin N+1 (la página de 50 cuesta lo mismo que la de 1) y las
    vistas de una empresa o de la plataforma en un número fijo de consultas."""
    from datetime import date

    from tests.billing_support import billing_url, company_with_plan, hire, pay, run_jobs, set_today
    from tests.conftest import create_employee

    set_today(monkeypatch, date(2026, 11, 1))
    companies = [
        company_with_plan(client, admin_headers, rfc=f"PNO12031{i}AB1", email=f"a{i}@p.com", starts_on="2026-09-01")
        for i in range(3)
    ]
    for company_id in companies:
        hire(company_id, 2, date(2026, 8, 1))
    run_jobs(date(2026, 10, 1))
    run_jobs(date(2026, 11, 1))  # dos cargos por empresa (septiembre y octubre)
    first = companies[0]
    for amount in ("10", "20", "30"):
        assert pay(client, admin_headers, first, amount, date(2026, 11, 1)).status_code == 201
    for i in range(3):
        create_employee(client, company_headers, number=f"U-{i}", email=f"u{i}@empresa.com")
    from app.services.usage_meter import UsageMeter

    meter = UsageMeter(1000)
    for user in range(1, 4):
        for route in ("GET /api/a", "GET /api/b"):
            meter.record(
                company_id=first,
                user_id=user,
                route=route,
                bytes_in=1,
                bytes_out=1,
                duration_ms=1,
                status=200,
                day=date(2026, 11, 1),
            )
    for company_id in companies:
        meter.record(
            company_id=company_id,
            user_id=1,
            route="GET /api/a",
            bytes_in=1,
            bytes_out=1,
            duration_ms=1,
            status=200,
            day=date(2026, 11, 1),
        )
    meter.flush()

    for url, budget in (
        ("/api/admin/billing/companies", "/api/admin/billing/companies"),
        ("/api/admin/billing/companies?overdue=true", "/api/admin/billing/companies"),
        (billing_url(first, "/charges"), "/api/admin/billing/companies/{id}/charges"),
        (billing_url(first, "/payments"), "/api/admin/billing/companies/{id}/payments"),
        (billing_url(first, "/statement"), "/api/admin/billing/companies/{id}/statement"),
        ("/api/admin/usage/companies?start=2026-11-01", "/api/admin/usage/companies"),
        (f"/api/admin/usage/companies/{first}/users?start=2026-11-01", "/api/admin/usage/companies/{id}/users"),
        (f"/api/admin/usage/companies/{first}/routes?start=2026-11-01", "/api/admin/usage/companies/{id}/routes"),
    ):
        assert_flat(client, url, admin_headers, budget)
    for url, budget in (
        ("/api/admin/billing/overview", "/api/admin/billing/overview"),
        (billing_url(first), "/api/admin/billing/companies/{id}"),
        ("/api/admin/usage/overview", "/api/admin/usage/overview"),
        (f"/api/admin/usage/companies/{first}", "/api/admin/usage/companies/{id}"),
    ):
        spent = cost(client, "GET", url, admin_headers)
        assert spent <= BUDGETS[budget], (url, spent)


def test_images_in_the_bucket_cost_no_extra_queries(client, company_headers, admin_headers, bucket):
    """La foto de referencia vive en el bucket: el detalle del registro no carga ninguna columna de bytes
    (su referencia viene en la misma fila) y el estado del almacenamiento del ADMIN son consultas fijas."""
    from app.core.database import SessionLocal
    from app.models import FaceEnrollment

    approved_employee(client, company_headers)
    with SessionLocal() as db:
        url = f"/api/enrollments/{db.query(FaceEnrollment.id).scalar()}"
    client.get(url, headers=company_headers)
    assert cost(client, "GET", url, company_headers) <= BUDGETS["GET /api/enrollments/{id}"]
    client.get("/api/admin/errors/server", headers=admin_headers)  # calienta la revisión de dependencias
    assert cost(client, "GET", "/api/admin/errors/server", admin_headers) <= BUDGETS["/api/admin/errors/server"]


def test_profile_photo_routes_fit_their_budget(client, company_headers):
    """Subir, leer (con el permiso de la empresa: la consulta más cara) y quitar una foto cuestan lo mismo siempre;
    el listado de empleados la trae sin consultas de más (la versión viaja en la cuenta, que ya se carga)."""
    from tests.avatar_support import upload
    from tests.conftest import create_employee, login

    people = []
    for i in range(3):
        email = f"f{i}@empresa.com"
        assert create_employee(client, company_headers, number=f"EMP-5{i:02d}", email=email).status_code == 201
        people.append(login(client, email, "Empleado123"))
    with count_queries() as statements:
        url = upload(client, people[0]).json()["data"]["avatar"]
    assert len(statements) == BUDGETS["PUT /api/users/me/avatar"], statements
    assert cost(client, "PUT", "/api/users/me/avatar", people[0], files={"file": ("f.jpg", _photo())}) == len(
        statements
    )  # reemplazar cuesta lo mismo que subir la primera
    for headers in people[1:]:
        upload(client, headers)
    photo_url = f"/api{client.get('/api/users/me', headers=people[0]).json()['data']['avatar']}&size=96"
    assert url and cost(client, "GET", photo_url, company_headers) <= BUDGETS["GET /api/users/{id}/avatar"]
    assert_flat(client, "/api/employees", company_headers, "/api/employees")
    assert cost(client, "DELETE", "/api/users/me/avatar", people[0]) <= BUDGETS["DELETE /api/users/me/avatar"]


def _photo() -> bytes:
    from tests.avatar_support import photo

    return photo()


def test_performance_screens_fit_their_budget(client, admin_headers, company_headers):
    """Rendimiento del ADMIN: listados sin N+1 y vistas en un número fijo de consultas; observar una petición (y
    sus alertas lentas) no agrega ninguna."""
    from tests.test_performance_api import ROUTE, seed

    seed()
    url = "/api/admin/performance"
    for path, budget in (
        (f"{url}/metrics?kind=HTTP&period=1h", f"{url}/metrics"),
        (f"{url}/metrics?kind=HTTP&period=24h&sort=p95", f"{url}/metrics"),
        (f"{url}/web-vitals?period=1h", f"{url}/web-vitals"),
        (f"{url}/alerts", f"{url}/alerts"),
        (f"{url}/alerts?status=OPEN", f"{url}/alerts"),
    ):
        assert_flat(client, path, admin_headers, budget)
    alert_id = client.get(f"{url}/alerts", headers=admin_headers).json()["data"]["items"][0]["id"]
    for path, budget in (
        (f"{url}/overview?period=24h", f"{url}/overview"),
        (f"{url}/metrics/series?kind=HTTP&period=6h&name={ROUTE}", f"{url}/metrics/series"),
        (f"{url}/statements", f"{url}/statements"),
        (f"{url}/alerts/summary", f"{url}/alerts/summary"),
        (f"{url}/alerts/{alert_id}", "GET /api/admin/performance/alerts/{id}"),
    ):
        spent = cost(client, "GET", path, admin_headers)
        assert spent <= BUDGETS[budget], (path, spent)
    patch = cost(client, "PATCH", f"{url}/alerts/{alert_id}", admin_headers, json={"status": "ACKNOWLEDGED"})
    assert patch <= BUDGETS["PATCH /api/admin/performance/alerts/{id}"], patch


def test_antifraud_screens_fit_their_budget(client, admin_headers, company_headers):
    """Casos de fraude y el historial de la política (ADMIN): listados sin N+1 y el detalle con sus intentos,
    historial y evidencia en un número fijo de consultas."""
    from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, login
    from tests.test_policy import set_policy
    from tests.test_risk_engine import verify
    from tests.test_validators import approved

    for person, number in (("ana", "EMP-001"), ("luis", "EMP-002"), ("eva", "EMP-003")):
        approved(client, company_headers, person, number=number)
        headers = login(client, f"{person}@empresa.com", "Empleado123")
        assert verify(client, headers, frontal=f"spoof:{person}".encode()).json()["code"] == "SPOOF_DETECTED"
    set_policy(client, company_headers, liveness_steps=3)
    set_policy(client, company_headers, lockout_minutes=30)
    admin = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)  # set_policy abrió otra sesión del ADMIN
    company_id = client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]
    assert_flat(client, "/api/admin/fraud-cases", admin, "/api/admin/fraud-cases")
    assert_flat(client, "/api/admin/fraud-cases?status=ALL", admin, "/api/admin/fraud-cases")
    changes = f"/api/admin/companies/{company_id}/verification-policy/changes"
    assert_flat(client, changes, admin, "/api/admin/companies/{id}/verification-policy/changes")
    count = cost(client, "GET", "/api/admin/fraud-cases/count", admin)
    assert count <= BUDGETS["/api/admin/fraud-cases/count"], count
    case_id = client.get("/api/admin/fraud-cases", headers=admin).json()["data"]["items"][0]["id"]
    detail = cost(client, "GET", f"/api/admin/fraud-cases/{case_id}", admin)
    assert detail <= BUDGETS["GET /api/admin/fraud-cases/{id}"], detail


def test_recording_with_the_face_fits_its_budget(client, company_headers, worker, monkeypatch):  # noqa: F811
    """Verificarse y registrar asistencia con la prueba del dispositivo, la telemetría y la galería del 1:N en memoria:
    un número fijo de consultas (sin contar el reto, que es otra petición)."""
    from tests.test_attendance import server_clock
    from tests.test_employee_devices import act_signed, verify

    staff, set_to = worker, server_clock(monkeypatch)  # Ana con su turno en la Planta Norte
    set_to(staff["day"], "07:50")
    act_signed(client, staff["headers"], "check-in")  # su dispositivo ya visto y la galería ya en memoria
    set_to(staff["day"], "12:00")
    challenge = client.post("/api/face/challenge", headers=staff["headers"]).json()["data"]
    with count_queries() as statements:
        response = act_signed(client, staff["headers"], "break-start", challenge=challenge)
    assert response.status_code == 200 and response.json()["data"]["verified"] is True
    assert len(statements) <= BUDGETS["POST /api/me/attendance/{action}"], statements
    challenge = client.post("/api/face/challenge", headers=staff["headers"]).json()["data"]
    with count_queries() as statements:
        response = verify(client, staff["headers"], person="ana", challenge=challenge)
    assert response.json()["data"]["verified"] is True
    assert len(statements) <= BUDGETS["POST /api/verification/face"], statements


def test_an_enrollment_costs_the_same_with_36_photos_as_with_3(client, company_headers):
    """El registro con muchas fotos (decisión del dueño, 2026-10-06): elegir las referencias es CPU; guardar las
    referencias es una inserción y recordar las huellas de todas las útiles, una sentencia (sin contar el reto)."""
    from tests.conftest import create_employee, enrollment_challenge, initial_photo, login, turn_files
    from tests.test_enrollment_selection import photo

    def cost_of(number: str, count: int) -> int:
        email = f"{number.lower()}@empresa.com"
        assert create_employee(client, company_headers, number=number, email=email).status_code == 201
        headers = login(client, email, "Empleado123")
        assert initial_photo(client, headers, photo(0, name=f"{number}~0.9~p")).status_code == 201
        challenge = enrollment_challenge(client, headers)
        images = [photo(i, name=f"{number}~0.9~v{i}") for i in range(count)]
        files = [("images", (f"f{i}.jpg", image, "image/jpeg")) for i, image in enumerate(images)]
        files += turn_files(challenge, number)
        data = {"challenge_id": challenge["challenge_id"]}
        with count_queries() as statements:
            response = client.post("/api/enrollment/face", data=data, files=files, headers=headers)
        assert response.status_code == 201, response.text
        return len(statements)

    cost_of("EMP-W", 3)  # calienta catálogos, política y umbrales
    three, many = cost_of("EMP-A", 3), cost_of("EMP-B", 36)
    assert three == many <= BUDGETS["POST /api/enrollment/face"], (three, many)


def test_presence_checks_fit_their_budget(client, company_headers):
    """Antifraude 2b: la firma por petición y la ubicación del validador no agregan consultas a la identificación; los
    kioscos de un sitio se listan sin N+1 y la tableta pide su código con un número fijo de consultas."""
    from tests.test_shifts import create_site
    from tests.test_site_codes import enable_code, fetch, new_kiosk, pair
    from tests.test_site_codes import signed as kiosk_signed
    from tests.test_validator_presence import FACE, identified, identify, profile, send, signature
    from tests.test_validators import approved

    approved(client, company_headers, "juan", number="EMP-001")
    headers = validator_headers(client, company_headers, mode="FACE")
    identified(identify(client, headers))  # la galería ya en memoria

    def spent(signed: dict | None) -> int:
        challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
        with count_queries() as statements:
            identified(send(client, headers, challenge, signed=signed))
        return len(statements)

    unsigned = spent(None)
    signed = spent(signature(profile(client, headers)["device_nonce"], "face", FACE))
    assert signed <= unsigned, (signed, unsigned)

    site = enable_code(client, company_headers, create_site(client, company_headers))
    codes = [new_kiosk(client, company_headers, site, f"Kiosco {i}")["pairing_code"] for i in range(3)]
    url = f"/api/sites/{site['id']}/kiosks"
    assert_flat(client, url, company_headers, "/api/sites/{id}/kiosks")
    for kiosk_id in (k["id"] for k in client.get(url, headers=company_headers).json()["data"]["items"][:2]):
        assert client.delete(f"{url}/{kiosk_id}", headers=company_headers).status_code == 200
    assert_flat(client, f"{url}?deleted=true", company_headers, "/api/sites/{id}/kiosks?deleted=true")
    session = pair(client, codes[0]).json()["data"]
    nonce = fetch(client, session["kiosk_id"], session["device_nonce"]).json()["data"]["device_nonce"]
    shown = cost(client, "POST", "/api/kiosk/code", {}, json=kiosk_signed(session["kiosk_id"], nonce))
    assert shown <= BUDGETS["POST /api/kiosk/code"], shown


def test_company_documents_fit_their_budget(client, admin_headers, company_headers):
    """Documentos de la empresa: listados sin N+1 (la página de 50 cuesta lo mismo que la de 1) y cada operación con
    un número fijo de consultas (el bucket nunca se espera con una transacción abierta)."""
    from tests.test_company_documents import admin_url, company_id_of, upload

    company_id = company_id_of(client, company_headers)
    for i in range(3):
        assert upload(client, company_headers, name=f"d{i}.pdf").status_code == 201
    assert_flat(client, "/api/documents", company_headers, "/api/documents")
    assert_flat(client, admin_url(company_id), admin_headers, "/api/admin/companies/{id}/documents")
    own = cost(client, "POST", "/api/documents", company_headers, **_document_form())
    platform = cost(client, "POST", admin_url(company_id), admin_headers, **_document_form())
    assert own <= BUDGETS["POST /api/documents"] and platform <= BUDGETS["POST /api/admin/companies/{id}/documents"]
    document_id = client.get("/api/documents", headers=company_headers).json()["data"]["items"][-1]["id"]
    url = f"/api/documents/{document_id}"
    assert cost(client, "GET", f"{url}/file", company_headers) <= BUDGETS["GET /api/documents/{id}/file"]
    admin_file = cost(client, "GET", f"{admin_url(company_id)}/{document_id}/file", admin_headers)
    assert admin_file <= BUDGETS["GET /api/admin/companies/{id}/documents/{id}/file"]
    assert cost(client, "DELETE", url, company_headers) <= BUDGETS["DELETE /api/documents/{id}"]
    other = client.get("/api/documents", headers=company_headers).json()["data"]["items"][-1]["id"]
    assert client.delete(f"/api/documents/{other}", headers=company_headers).status_code == 200
    assert_flat(client, "/api/documents?deleted=true", company_headers, "/api/documents?deleted=true")
    assert cost(client, "POST", f"{url}/restore", company_headers) <= BUDGETS["POST /api/documents/{id}/restore"]


def _document_form() -> dict:
    from tests.document_support import PDF

    return {"files": {"file": ("constancia.pdf", PDF, "application/pdf")}, "data": {"type": "TAX_CERTIFICATE"}}


def test_the_company_tax_id_fits_its_budget(client, admin_headers):
    """Alta, edición, validación en vivo y restauración con un identificador fiscal: una revisión cada una."""
    body = {"name": "Acme", "legal_name": "Acme SA", "phone": "6621234567", "admin_email": "x@x.com"}
    tax = {"tax_country": "US", "tax_id_type": "US_EIN", "tax_id": "12-3456789"}
    client.get("/api/catalogs", headers=admin_headers)  # calienta los catálogos (el tipo y su regla)
    with count_queries() as statements:
        created = client.post(
            "/api/admin/companies", json={**body, **tax, "admin_password": "Empresa1234"}, headers=admin_headers
        )
    assert created.status_code == 201 and len(statements) <= BUDGETS["POST /api/admin/companies"], statements
    url = f"/api/admin/companies/{created.json()['data']['id']}"
    edited = cost(client, "PUT", url, admin_headers, json={**tax, "tax_id": "987654321"})
    assert edited <= BUDGETS["PUT /api/admin/companies/{id}"], edited
    params = {"field": "company_tax_id", "value": "987654321", "related": "US:US_EIN"}
    checked = cost(client, "GET", "/api/validation", admin_headers, params=params)
    assert checked <= BUDGETS["/api/validation?field=company_tax_id"], checked
    assert client.delete(url, headers=admin_headers).status_code == 200
    restored = cost(client, "POST", f"{url}/restore", admin_headers)
    assert restored <= BUDGETS["POST /api/admin/companies/{id}/restore"], restored


def test_drift_and_passkey_routes_fit_their_budget(client, admin_headers, company_headers):
    """Deriva de señales (ADMIN): listados por ventana sin N+1 (la página de 50 cuesta lo mismo que la de 1) y el
    resumen en un número fijo de consultas; llaves de acceso: la lista propia y el inicio de sesión con una."""
    from datetime import UTC, datetime

    from app.core.database import SessionLocal
    from app.services import drift_service
    from tests.passkey_support import FakeAuthenticator, login_with, register
    from tests.test_drift import WEEK

    with SessionLocal() as db:
        drift_service.compute_window(db, WEEK, datetime.now(UTC))
    assert_flat(client, "/api/admin/drift", admin_headers, "/api/admin/drift")
    assert cost(client, "GET", "/api/admin/drift/companies", admin_headers) <= BUDGETS["/api/admin/drift/companies"]
    summary = cost(client, "GET", "/api/admin/drift/summary", admin_headers)
    assert summary <= BUDGETS["/api/admin/drift/summary"], summary
    for i in range(3):
        assert register(client, company_headers, FakeAuthenticator(), name=f"Llave {i}").status_code == 201
    listed = cost(client, "GET", "/api/auth/passkeys?size=50", company_headers)
    assert listed <= BUDGETS["/api/auth/passkeys"], listed
    device = FakeAuthenticator()
    assert register(client, company_headers, device).status_code == 201
    with count_queries() as statements:
        assert login_with(client, device).status_code == 200
    assert len(statements) <= BUDGETS["POST /api/auth/login/passkey"], statements


def test_the_mobile_verification_api_fits_its_budget(client, company_headers):
    """La API pública de verificación (SDK móviles): el reto, el 1:1 y el 1:N con la galería en memoria cuestan un
    número fijo de consultas (las del reto aparte del envío)."""
    from tests.test_api_keys import new_key
    from tests.test_verification_api import attempt, challenge

    approved_employee(client, company_headers)
    secret = new_key(client, company_headers, scopes=["VERIFICATION"])["secret"]
    attempt(client, secret)  # calienta catálogos, política, umbrales y el último uso de la llave
    attempt(client, secret, action="identify")  # la galería ya en memoria
    with count_queries() as statements:
        issued = challenge(client, secret)
    assert issued.status_code == 200
    assert len(statements) <= BUDGETS["POST /api/integrations/v1/verification/challenge"], statements
    for action in ("verify", "identify"):
        issued = challenge(client, secret).json()["data"]
        with count_queries() as statements:
            response = attempt(client, secret, action=action, issued=issued)
        assert response.json()["data"]["decision"] == "ALLOW", response.text
        assert len(statements) <= BUDGETS[f"POST /api/integrations/v1/verification/{action}"], statements
