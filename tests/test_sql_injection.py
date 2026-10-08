"""Matriz de inyección SQL (regla 21 del `AGENTS.md` raíz), como `tests/test_authorization.py`: TODAS las rutas.

Lee el OpenAPI de la app y, en cada ruta de `API_ROUTERS`, pone cada carga clásica (`PAYLOADS`: comillas, `OR 1=1`,
`DROP TABLE`, comodines de `LIKE`, barra invertida, comillas Unicode, marcadores de parámetros, NUL, textos muy largos)
en CADA parámetro de texto de la query y de la ruta y en CADA campo de texto del cuerpo (JSON a cualquier nivel o
formulario), uno por petición y con lo demás lleno de valores válidos. Llama con quien SÍ tiene permiso (los actores de
la empresa A de `tests/test_tenant_isolation.py`, el ADMIN en sus rutas, la llave de integración de A), así la carga
llega a los servicios y a la base. Verifica:

- nunca un 5xx: el resultado normal o un 4xx con el contrato de siempre;
- NUL (y los demás controles) → 422 `INVALID_CHARACTERS` en TODA ruta, antes de cualquier otra cosa;
- ninguna respuesta a A trae algo de la empresa B (cada dato de B lleva la marca "ZZB", `B_DATA`);
- la base queda intacta: las mismas tablas, ninguna fila de menos en las tablas con borrado lógico y las filas de B
  idénticas byte a byte.

También el canal en vivo, la cabecera `X-API-Key` de la integración, las búsquedas (un `%`, un `_` o una `\\` se
buscan literales) y que un error de la base nunca le muestre SQL ni parámetros a quien llama. Corre en la suite rápida
(SQLite) y en PostgreSQL con el usuario de la API y la seguridad por fila (`TEST_DATABASE_URL`).
"""

import copy
import json
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import DataError, OperationalError
from starlette.testclient import WebSocketDenialResponse

from app.core.config import settings
from app.core.database import Base, SessionLocal
from app.core.db_schemas import ALL_SCHEMAS
from app.core.row_security import TENANT_TABLES
from app.main import API_ROUTERS, app
from app.middleware.rate_limit import limiter
from app.models import Department, ErrorReport, UserRole
from app.repositories.department_repository import DepartmentRepository
from app.services.error_reporter import error_reporter
from tests.conftest import owner_engine
from tests.test_authorization import ANY_SESSION, PUBLIC, _allowed_roles, _calls, _named
from tests.test_realtime import URL as WS_URL
from tests.test_realtime import token
from tests.test_tenant_isolation import tenants  # noqa: F401  (fixture: las empresas A y B)

PREFIX = "/api"
NUL = "a\x00b"
ESCAPE = "\x1b[31m"
#: Las cargas que la API no acepta en ningún texto (422 `INVALID_CHARACTERS`).
INVALID = (NUL, ESCAPE)
#: Las cargas: cada una en cada campo de texto de cada ruta.
PAYLOADS = (
    "' OR '1'='1",
    "'; DROP TABLE workforce.employees; --",
    "1 OR 1=1",
    "1); DELETE FROM auth.users; --",
    "%",
    "_",
    "\\",
    '" OR ""="',
    "ʼ OR ʼ1ʼ=ʼ1",  # U+02BC
    "’ OR ‘1’=‘1",  # comillas tipográficas
    "＇ OR ＇1＇=＇1",  # comilla de ancho completo
    "%(company_id)s $1 :company_id {0}",  # marcadores de parámetros de psycopg, PostgreSQL, SQLAlchemy y format
    "*/ UNION SELECT password_hash FROM auth.users --",
    NUL,
    ESCAPE,
    "x" * 10_000,
)
#: Un dato de la empresa B en una respuesta: sus textos empiezan con "ZZB " (`tests/test_tenant_isolation.py`), sus
#: correos con "zzb...@" y su número de empleado es "ZZB-001". Solo las tres letras no bastan: aparecen por azar en un
#: token aleatorio (JWT, retos), y la prueba fallaría sin una fuga.
B_DATA = re.compile(r"zzb(?: |-0|[a-z]*@)", re.IGNORECASE)
#: Las tablas con borrado lógico: ninguna petición puede quitarles filas (solo las marca), con o sin inyección.
SOFT_DELETED = (
    "tenancy.companies",
    "auth.users",
    "workforce.employees",
    "workforce.validators",
    "workforce.departments",
    "workforce.work_sites",
    "workforce.shifts",
    "workforce.shift_assignments",
    "workforce.company_holidays",
    "workforce.employee_workdays",
)
#: Orden de los actores: el ADMIN al final (sus rutas cambian a la empresa A: suspenderla cierra sus sesiones).
ORDER = ("employee", "validator", "key", "public", "company", "admin")
ROLE_ACTOR = {
    UserRole.EMPLOYEE: "employee",
    UserRole.VALIDATOR: "validator",
    UserRole.COMPANY: "company",
    UserRole.ADMIN: "admin",
}


# ----------------------------------------------------------------------------------------- valores desde el OpenAPI


class Schema:
    """Lee el OpenAPI de la app: resuelve `$ref`, arma valores válidos y enumera los campos de texto."""

    def __init__(self, document: dict) -> None:
        self.components = document.get("components", {}).get("schemas", {})

    def resolve(self, schema: dict) -> dict:
        while "$ref" in schema:
            schema = self.components[schema["$ref"].rsplit("/", 1)[1]]
        for key in ("anyOf", "oneOf"):
            options = [o for o in schema.get(key, []) if self.resolve(o).get("type") != "null"]
            if options:
                merged = {k: v for k, v in schema.items() if k != key}
                return {**self.resolve(options[0]), **merged}
        if schema.get("allOf"):
            return self.resolve(schema["allOf"][0])
        return schema

    def value(self, schema: dict, depth: int = 0) -> Any:
        """Un valor válido (los obligatorios, recursivo): el ejemplo o el valor por omisión si los hay."""
        schema = self.resolve(schema)
        for key in ("const", "default"):
            if schema.get(key) is not None:
                return copy.deepcopy(schema[key])
        if schema.get("enum"):
            return schema["enum"][0]
        if schema.get("examples"):
            return copy.deepcopy(schema["examples"][0])
        kind = schema.get("type")
        if kind == "object" or "properties" in schema:
            required = schema.get("required", [])
            properties = schema.get("properties", {})
            return {} if depth > 4 else {name: self.value(properties[name], depth + 1) for name in required}
        if kind == "array":
            return [self.value(schema.get("items", {}), depth + 1) for _ in range(schema.get("minItems", 0))]
        if kind == "integer":
            return max(int(schema.get("minimum", 1)), int(schema.get("exclusiveMinimum", 0)) + 1)
        if kind == "number":
            return max(float(schema.get("minimum", 1)), float(schema.get("exclusiveMinimum", 0)) + 1)
        if kind == "boolean":
            return False
        return self._text(schema)

    @staticmethod
    def _text(schema: dict) -> str:
        today = datetime.now(UTC)
        by_format = {
            "email": "matriz@ejemplo.com",
            "date": today.date().isoformat(),
            "date-time": today.isoformat(),
            "time": "09:00:00",
            "uuid": "00000000-0000-4000-8000-000000000000",
        }
        text_value = by_format.get(schema.get("format", ""), "valor")
        return text_value.ljust(int(schema.get("minLength", 0)), "v")[: schema.get("maxLength", 10_000)]

    def text_fields(self, schema: dict, path: tuple = (), depth: int = 0) -> Iterator[tuple]:
        """La ruta de cada campo de texto (a cualquier nivel; en una lista, su primer elemento)."""
        schema = self.resolve(schema)
        if depth > 4:
            return
        if schema.get("type") == "object" or "properties" in schema:
            for name, sub in schema.get("properties", {}).items():
                yield from self.text_fields(sub, (*path, name), depth + 1)
        elif schema.get("type") == "array":
            yield from self.text_fields(schema.get("items", {}), (*path, 0), depth + 1)
        elif schema.get("type") == "string" and not _binary(schema):
            yield path

    def put(self, schema: dict, path: tuple, payload: str, depth: int = 0) -> Any:
        """El valor válido del esquema con `payload` en `path` (crea lo que haga falta en el camino)."""
        if not path:
            return payload
        schema = self.resolve(schema)
        head, rest = path[0], path[1:]
        if isinstance(head, int):
            return [self.put(schema.get("items", {}), rest, payload, depth + 1)]
        built = self.value(schema, depth) if depth <= 4 else {}
        built = built if isinstance(built, dict) else {}
        built[head] = self.put(schema.get("properties", {}).get(head, {}), rest, payload, depth + 1)
        return built


SCHEMA = Schema(app.openapi())


def _binary(schema: dict) -> bool:
    """Un archivo del formulario (OpenAPI 3.1: `contentMediaType`; antes, `format: binary`)."""
    schema = SCHEMA.resolve(schema)
    if schema.get("type") == "array":
        return _binary(schema.get("items", {}))
    return schema.get("format") == "binary" or "contentMediaType" in schema


def _is_text(parameter: dict) -> bool:
    schema = SCHEMA.resolve(parameter.get("schema", {}))
    return schema.get("type") == "string"


def _actor(method: str, path: str, calls: list) -> str:
    if (method, path) in PUBLIC:
        return "public"
    if (method, path) in ANY_SESSION:
        return "company"
    if _named(calls, "get_api_client"):
        return "key"
    allowed = _allowed_roles(calls) or set()
    for role in (UserRole.COMPANY, UserRole.EMPLOYEE, UserRole.VALIDATOR, UserRole.ADMIN):
        if role in allowed:
            return ROLE_ACTOR[role]
    raise AssertionError(f"{method} {path}: sin rol permitido")  # test_authorization ya lo impide


def routes() -> list[tuple[str, str, str]]:
    """(actor, método, ruta) de cada ruta de la API, en el orden de `ORDER`."""
    found = []
    for router in API_ROUTERS:
        for route in router.routes:
            if isinstance(route, APIRoute):
                for method in sorted(route.methods - {"HEAD"}):
                    found.append((_actor(method, route.path, _calls(route.dependant)), method, route.path))
    return sorted(found, key=lambda item: (ORDER.index(item[0]), item[2], item[1]))


class Request:
    """Una petición a una ruta con una carga en uno de sus campos (o en ninguno: `target=None`)."""

    def __init__(self, method: str, path: str) -> None:
        self.method, self.path = method, path
        self.operation = app.openapi()["paths"][PREFIX + path][method.lower()]
        self.parameters = [p for p in self.operation.get("parameters", []) if p["in"] in ("query", "path")]
        body = self.operation.get("requestBody", {}).get("content", {})
        self.media, self.body = next(iter(body.items()), (None, None))
        if self.body is not None:  # un cuerpo con su propio esquema (`openapi_extra`) trae sus `$defs`
            SCHEMA.components.update(self.body["schema"].get("$defs", {}))

    def targets(self) -> Iterator[tuple]:
        for parameter in self.parameters:
            if parameter["in"] == "path" or _is_text(parameter):
                yield (parameter["in"], parameter["name"])
        if self.body is not None:
            for path in SCHEMA.text_fields(self.body["schema"]):
                yield ("body", *path)

    def build(self, target: tuple | None, payload: str, ids: Mapping[str, object] | None = None) -> dict[str, Any]:
        """`ids`: el valor de cada parámetro de la ruta por su nombre (sin él, "1"); `tests/test_api_language.py` lo
        usa para llegar a los datos de verdad."""
        url = PREFIX + self.path
        query: dict[str, Any] = {}
        for parameter in self.parameters:
            hit = target == (parameter["in"], parameter["name"])
            if parameter["in"] == "path":
                value = quote(payload, safe="") if hit else str((ids or {}).get(parameter["name"], "1"))
                url = url.replace("{" + parameter["name"] + "}", value)
            elif hit:
                query[parameter["name"]] = payload
            elif parameter.get("required"):
                query[parameter["name"]] = SCHEMA.value(parameter.get("schema", {}))
        request: dict[str, Any] = {"url": url, "params": query}
        if self.body is not None:
            schema = self.body["schema"]
            body = SCHEMA.put(schema, target[1:], payload) if target and target[0] == "body" else SCHEMA.value(schema)
            if self.media == "application/json":
                request["json"] = body
            else:  # formulario: los archivos con un contenido cualquiera, los textos como campos
                request["data"], request["files"] = self._form(schema, body)
        return request

    @staticmethod
    def _form(schema: dict, body: dict) -> tuple[dict, list]:
        properties = SCHEMA.resolve(schema).get("properties", {})
        data, files = {}, []
        for name, sub in properties.items():
            if _binary(sub):
                files.append((name, ("captura.jpg", b"face:matriz", "image/jpeg")))
            elif name in body:
                value = body[name]
                data[name] = value if isinstance(value, str) else json.dumps(value)
        return data, files


# ------------------------------------------------------------------------------------------- estado de la base


def _snapshot(b_company: int) -> dict[str, Any]:
    """Las tablas de la base (el dueño ve todas), las filas de B (todas sus tablas de empresa) y cuántas filas tienen
    las tablas con borrado lógico (sesión de la plataforma de las pruebas: cruza empresas)."""
    inspector = inspect(owner_engine)
    postgres = owner_engine.dialect.name == "postgresql"
    tables = sorted(
        f"{schema}.{name}"
        for schema in (ALL_SCHEMAS if postgres else ("",))
        for name in inspector.get_table_names(schema or None)
    )
    with SessionLocal() as db:
        rows = {
            name: sorted(
                tuple(map(repr, row)) for row in db.execute(select(table).where(table.c.company_id == b_company))
            )
            for name, table in ((n, Base.metadata.tables[n]) for n in sorted(TENANT_TABLES))
        }
        counts = {
            name: db.scalar(select(func.count()).select_from(Base.metadata.tables[name])) for name in SOFT_DELETED
        }
    return {"tables": tables, "rows": rows, "counts": counts}


def _check(response, *, payload: str, label: str, actor: str) -> list[str]:
    problems = []
    if response.status_code >= 500:
        problems.append(f"{label} → {response.status_code}")
    is_json = response.headers.get("content-type", "").startswith("application/json")
    body = response.json() if is_json else None
    if is_json and response.status_code >= 400 and body.get("success") is not False:
        problems.append(f"{label} → {response.status_code} sin el contrato")
    if payload in INVALID and (response.status_code != 422 or (body or {}).get("code") != "INVALID_CHARACTERS"):
        problems.append(f"{label} con un control → {response.status_code} {(body or {}).get('code')}")
    if actor != "admin" and B_DATA.search(response.text):
        problems.append(f"{label} → muestra datos de la empresa B")
    return problems


@pytest.fixture
def actors(tenants, admin_headers) -> dict[str, dict[str, str]]:  # noqa: F811
    a = tenants["a"]
    return {
        "employee": a["employee"],
        "validator": a["validator"],
        "company": a["company"],
        "admin": admin_headers,
        "key": {"X-API-Key": a["key"]},
        "public": {},
    }


def test_every_route_resists_injection_in_every_text_field(client, tenants, actors):  # noqa: F811
    before = _snapshot(tenants["b_company"])
    problems, reached = [], 0
    for actor, method, path in routes():
        request = Request(method, path)
        for target in request.targets():
            for payload in PAYLOADS:
                limiter.reset()  # cada petición con su cuota completa: que la carga llegue al servicio, no al límite
                label = f"{actor} {method} {path} [{'.'.join(map(str, target))}={payload[:20]!r}]"
                response = client.request(method, headers=actors[actor], **request.build(target, payload))
                reached += response.status_code < 400
                problems += _check(response, payload=payload, label=label, actor=actor)
    assert problems == [], "\n".join(problems[:60])
    assert reached > 100  # la matriz llegó de verdad a los servicios (no todo se quedó en la validación)
    after = _snapshot(tenants["b_company"])
    assert after["tables"] == before["tables"], "cambiaron las tablas de la base"
    assert after["rows"] == before["rows"], "cambiaron datos de la empresa B"
    shrunk = {t: (before["counts"][t], n) for t, n in after["counts"].items() if n < before["counts"][t]}
    assert shrunk == {}, f"tablas con borrado lógico que perdieron filas: {shrunk}"


def test_every_route_rejects_nul_and_control_characters_before_anything_else(client):
    """Sin sesión ni datos: la regla corre antes que la autenticación (ningún texto inválido llega a un servicio)."""
    problems = []
    for _, method, path in routes():
        request = Request(method, path)
        for character in ("\x00", "\x07", "\ud800"):
            params = {"q": f"a{character}b"} if character != "\ud800" else {"q": "a"}
            built = request.build(None, "")
            built["params"] = {**built["params"], **params}
            if character == "\ud800":
                if request.media != "application/json":
                    continue
                built["content"] = '{"campo": "\\ud800"}'
                built.pop("json", None)
                built["headers"] = {"Content-Type": "application/json"}
            response = client.request(method, **built)
            if response.status_code != 422 or response.json()["code"] != "INVALID_CHARACTERS":
                problems.append(f"{method} {path} {character!r} → {response.status_code} {response.text[:80]}")
    assert problems == [], "\n".join(problems[:40])


# ---------------------------------------------------------------------------------- canal en vivo e integración


def test_the_live_channel_answers_every_payload_without_failing(client, company_headers, monkeypatch):
    monkeypatch.setattr(settings, "WS_MAX_MESSAGES_PER_10S", 1000)  # todas las cargas, sin el límite del canal
    with client.websocket_connect(WS_URL) as socket:
        socket.send_json({"type": "auth", "token": token(client)})
        assert socket.receive_json()["code"] == "WS_AUTHENTICATED"
        for payload in [p for p in PAYLOADS if len(p) <= 255]:  # el canal acepta hasta 255 caracteres por valor
            for message in (
                {"type": "validate", "id": "req-00000001", "field": "email", "value": payload},
                {"type": "validate", "id": "req-00000001", "field": "phone", "value": "6621234567", "related": payload},
                {"type": "validate", "id": "req-00000001", "field": payload, "value": "x"},
                {"type": "validate", "id": payload, "field": "department_name", "value": payload},
                {"type": "flash", "id": "req-00000001", "token": payload, "digest": payload},
            ):
                socket.send_json(message)
                answer = socket.receive_json()
                assert answer["statusCode"] < 500, (message, answer)
                if payload in INVALID:
                    assert answer["code"] == "INVALID_CHARACTERS", (message, answer)
        socket.send_json({"type": "ping"})
        assert socket.receive_json()["code"] == "PONG"  # el canal sigue abierto


def test_the_live_channel_refuses_a_url_with_control_characters(client):
    with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect(f"{WS_URL}?lang=es%00"):
        pass  # no llega: la conexión se rechaza al abrirse
    assert denied.value.status_code == 422 and denied.value.json()["code"] == "INVALID_CHARACTERS"


def test_the_integration_key_header_never_breaks_the_api(client, tenants):  # noqa: F811
    for payload in [p for p in PAYLOADS if p.isascii() and p not in INVALID]:
        response = client.get(f"{PREFIX}/integrations/v1/employees", headers={"X-API-Key": payload})
        assert response.status_code == 401 and response.json()["code"] in ("API_KEY_INVALID", "API_KEY_REQUIRED")
    for payload in INVALID:  # un control en una cabecera: ni siquiera se busca la llave
        response = client.get(f"{PREFIX}/integrations/v1/employees", headers={"X-API-Key": payload})
        assert response.status_code == 422 and response.json()["errors"][0]["field"] == "x-api-key"
    response = client.get(f"{PREFIX}/integrations/v1/employees", headers={"X-API-Key": tenants["a"]["key"]})
    assert response.status_code == 200 and not B_DATA.search(response.text)


# ------------------------------------------------------------------------------------------------- búsquedas


def test_search_wildcards_and_quotes_are_literal(client, company_headers):
    names = ("Ventas 100%", "Ventas_Norte", "Ventas Sur", "Archivo\\Central", "Planta/Oeste", "O'Higgins")
    for name in names:
        assert client.post(f"{PREFIX}/departments", json={"name": name}, headers=company_headers).status_code == 201

    def found(term: str) -> list[str]:
        response = client.get(f"{PREFIX}/departments", params={"search": term}, headers=company_headers)
        assert response.status_code == 200, response.text
        return sorted(item["name"] for item in response.json()["data"]["items"])

    assert found("%") == ["Ventas 100%"]
    assert found("_") == ["Ventas_Norte"]
    assert found("\\") == ["Archivo\\Central"]
    assert found("/") == ["Planta/Oeste"]
    assert found("'") == ["O'Higgins"]
    assert found("ventas%sur") == [] and found("ventas_sur") == []  # nunca comodines
    assert found("' OR '1'='1") == [] and found("%' OR 1=1 --") == []
    assert found("ventas") == ["Ventas 100%", "Ventas Sur", "Ventas_Norte"]
    with SessionLocal() as db:  # el repositorio también, sin pasar por la ruta
        company_id = db.scalar(select(Department.company_id).limit(1))
        assert DepartmentRepository(db, company_id).search(search=" _ ", offset=0, limit=10)[1] == 1


# --------------------------------------------------------------------------------------------- errores de la base


_LEAKY_SQL = "SELECT password_hash FROM auth.users WHERE email = %(email)s"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (DataError(_LEAKY_SQL, {"email": "fuga@secreta.mx"}, Exception("x"), True), 500, "INTERNAL_ERROR"),
        (OperationalError(_LEAKY_SQL, {"email": "fuga@secreta.mx"}, Exception("x"), True), 503, "DATABASE_UNAVAILABLE"),
    ],
    ids=["500", "503"],
)
def test_a_database_error_never_shows_sql_or_its_parameters(client, company_headers, monkeypatch, error, status, code):
    """A quien llama: el mensaje del catálogo, nunca la sentencia ni sus valores. Al ADMIN (bandeja de errores): la
    sentencia de una excepción no controlada para reproducirla, pero sus valores no (`hide_parameters` del motor)."""

    def failing(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(DepartmentRepository, "search", failing)
    response = client.get(f"{PREFIX}/departments", params={"search": "ventas"}, headers=company_headers)
    assert response.status_code == status and response.json()["code"] == code
    assert not re.search(r"SELECT|password_hash|auth\.users|fuga@secreta|%\(email\)s", response.text)
    error_reporter.flush()
    with SessionLocal() as db:
        stored = " ".join(
            f"{message} {detail}" for message, detail in db.execute(select(ErrorReport.message, ErrorReport.detail))
        )
    assert stored and "fuga@secreta.mx" not in stored
    assert status == 503 or "parameters hidden" in stored
