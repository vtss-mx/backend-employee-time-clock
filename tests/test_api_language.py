"""Idioma estricto de la API (regla 16 de la raíz). Decisión del dueño del producto: «no mezcles el spanish con el
english… todo el idioma debe ser en caliente». Ningún texto que lee una persona sale en un idioma distinto del que
pidió (`Accept-Language`; el canal en vivo, `?lang=`), y el sobre trae cada texto en CADA idioma (`i18n`) para que la
aplicación web cambie de idioma un aviso abierto sin repetir la petición.

**Cómo se decide el idioma de un texto: por su PROCEDENCIA, no por su ortografía.** Cada texto que la API escribe sale
del catálogo de mensajes (`app/i18n/messages/`, una plantilla por llave y forma de plural) o de un catálogo de la BD
(`alembic/seed/catalogs.json` en es-MX y `catalogs.en-US.json` en en-US). Un texto "es de un idioma" si es
exactamente un texto de los catálogos de ese idioma o si encaja en una de sus plantillas (cada `{parámetro}` es un
comodín, de principio a fin). Es determinista y exacto, y más fuerte que un corrector ortográfico: una palabra válida
en los dos idiomas («Error», «Total», «Panel») no puede esconder una fuga, porque lo que se revisa es de dónde salió
la frase completa, no si sus palabras existen. Y no necesita Node en la imagen de Python (cspell revisa la ortografía
de los catálogos aparte, `cspell.json`). Reglas:

- Una plantilla que solo tiene parámetros («{text}.», «{first} {second}») no basta por sí sola: al menos uno de sus
  parámetros debe ser, a su vez, un texto del mismo idioma (se resuelve el texto anidado).
- Ningún parámetro de una plantilla puede ser un texto que solo existe en el OTRO idioma («No se registró: You're on
  vacation» es una fuga aunque la plantilla sea española).
- Lo que no tiene palabras (números, fechas, horas) es de cualquier idioma.

**Qué se revisa:**
1. TODAS las rutas de `API_ROUTERS` (la misma matriz de `tests/test_sql_injection.py`: su lector del OpenAPI arma
   valores válidos) con el rol que SÍ tiene permiso, una vez en es-MX y otra en en-US (cada idioma con la base desde
   cero). Los actores son los de la empresa con datos de todo tipo de `tests/test_tenant_isolation.py` (sus ids reales
   en la ruta: así se llega a los datos y no solo a un 404). En cada respuesta:
   - `message` y cada `errors[].message` son del idioma pedido;
   - `i18n[idioma]` (`message`, `errors` y `texts`, los textos de `data` de lo que no se puede volver a pedir) es de
     ESE idioma para cada idioma de la API, `texts` tiene el mismo largo en todos y `i18n[pedido]` es idéntico a
     `message`/`errors`;
   - ningún texto dentro de `data` (a cualquier profundidad) ni de `errors[].details` es un texto conocido SOLO del
     otro idioma (un texto exacto de sus catálogos o una plantilla suya con al menos dos palabras propias: sin
     ambigüedad). Lo que escribe una persona (nombres, notas) es un dato y no se revisa.
2. Los sobres de error de cada capa en los dos idiomas: 400, 401, 403, 404, 405, 413, 422 (Pydantic y de negocio),
   409, 429, 500 (de un middleware y no controlado) y 503 (BD caída, saturación, apagado, salud).
3. El canal en vivo (`/api/ws/validation?lang=`): el sobre de cada mensaje y los textos del resultado de la
   validación (`data.message`).

Cada fuga se informa con el método, la ruta, el idioma, el lugar en el JSON y el texto. Corre con `strict(True)`
(`tests/conftest.py`): una llave o un parámetro que falte también hace fallar la prueba.
"""

import re
import string
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import cache
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core import lifecycle
from app.core.admission import admission
from app.core.config import settings
from app.core.database import SessionLocal
from app.i18n import LOCALES
from app.i18n.messages import MESSAGES
from app.middleware.rate_limit import limiter
from app.models import User
from app.models.catalog import TRANSLATED_FIELDS
from app.models.catalog_seed import load_catalog_seed, load_translation_seed
from app.repositories.department_repository import DepartmentRepository
from app.services import health_service
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, create_employee, login
from tests.test_realtime import URL as WS_URL
from tests.test_sql_injection import ORDER, Request, routes
from tests.test_tenant_isolation import tenants  # noqa: F401  (fixture: la empresa B, con datos de todo tipo)
from tests.test_validators import PASSWORD as VALIDATOR_PASSWORD

#: Los otros idiomas de cada idioma (con dos: el otro).
OTHERS = {locale: tuple(other for other in LOCALES if other != locale) for locale in LOCALES}
#: Una palabra con contenido (tres letras o más): «AM», «PM», «MB» o «ID» no deciden un idioma.
WORD = re.compile(r"[^\W\d_]{3,}")
#: Las palabras propias de una plantilla que cuentan para reconocerla sin ambigüedad (dos letras o más).
LITERAL_WORD = re.compile(r"[^\W\d_]{2,}")
#: Lo más largo que se revisa dentro de `data` (un archivo en base64 no es un texto para una persona).
DATA_TEXT_MAX = 2_000
#: Cuántas maneras de repartir un texto entre los parámetros de una plantilla se prueban como máximo.
ASSIGNMENTS_MAX = 64
#: Los parámetros que llevan un DATO (un nombre, un número, un correo, la fecha, los elementos de una lista): en una
#: plantilla solo de parámetros no se exige que sean del idioma; los demás («{text}», «{first}», «{message}»...) sí.
DATA_PARAMS = frozenset({"name", "number", "email", "items", "last", "value", "date", "time"})
#: Los parámetros que siempre son una cifra (`count` elige el plural y lleva separador de miles): solo encajan cifras
#: («{count} registro facial» no puede ser «Fotos de referencia del registro facial»).
NUMBER_PARAMS = frozenset({"count", "days", "length", "max", "min", "height", "width", "id", "sequence"})
NUMBER = re.compile(r"[\d,.]+")


# ------------------------------------------------------------------------------------- índice de procedencia


@dataclass(frozen=True)
class Template:
    """Una plantilla del catálogo: sus partes (texto propio o parámetro), en orden."""

    key: str
    parts: tuple[tuple[bool, str], ...]
    #: Los trozos de texto propio (todos deben aparecer en el texto: descarta en microsegundos).
    literals: tuple[str, ...]
    #: Los nombres de sus parámetros, en orden.
    params: tuple[str, ...]
    #: Su texto propio tiene una palabra con contenido (si no, solo parámetros: «{text}.»).
    worded: bool
    #: Cuántas palabras propias tiene (para reconocerla sin ambigüedad dentro de `data`).
    words: int


def _template(key: str, text: str) -> Template:
    parts: list[tuple[bool, str]] = []
    for literal, name, _spec, _conversion in string.Formatter().parse(text):
        if literal:
            parts.append((False, literal))
        if name is not None:
            parts.append((True, name))
    literal_text = "".join(value for is_param, value in parts if not is_param)
    return Template(
        key=key,
        parts=tuple(parts),
        literals=tuple(value for is_param, value in parts if not is_param),
        params=tuple(value for is_param, value in parts if is_param),
        worded=bool(WORD.search(literal_text)),
        words=len(LITERAL_WORD.findall(literal_text)),
    )


def _forms(message: str | Any) -> list[str]:
    return [message] if isinstance(message, str) else list(message.values())


@dataclass(frozen=True)
class Index:
    """Los textos de un idioma: los exactos (sin parámetros) y las plantillas (con parámetros)."""

    exact: frozenset[str]
    #: Los exactos del catálogo de mensajes (una llave sin parámetros).
    messages: frozenset[str]
    templates: tuple[Template, ...]


def _catalog_texts() -> dict[str, list[str]]:
    """Los textos de los catálogos de la BD de cada idioma: el español en sus columnas, los demás en su archivo."""
    spanish = [
        str(row[field])
        for rows in load_catalog_seed().values()
        for row in rows
        for field in TRANSLATED_FIELDS
        if isinstance(row.get(field), str) and row[field]
    ]
    texts = {"es-MX": spanish}
    for locale, catalogs in load_translation_seed().items():
        texts[locale] = [text for codes in catalogs.values() for fields in codes.values() for text in fields.values()]
    return texts


@cache
def index(locale: str) -> Index:
    catalog = _catalog_texts()[locale]
    entries = [(f"catalog:{i}", text) for i, text in enumerate(catalog)]
    entries += [(key, text) for key, message in MESSAGES[locale].items() for text in _forms(message)]
    exact, messages, templates = set(), set(), []
    for key, text in entries:
        template = _template(key, text)
        if template.params:
            templates.append(template)
        else:
            literal = "".join(template.literals)  # como se escribe (`{{` → `{`)
            exact.add(literal)
            if not key.startswith("catalog:"):
                messages.add(literal)
    return Index(frozenset(exact), frozenset(messages), tuple(templates))


def _assignments(parts: tuple[tuple[bool, str], ...], text: str, start: int = 0) -> Iterator[list[str]]:
    """Cada manera de repartir `text` entre los parámetros de la plantilla (los trozos propios, tal cual)."""
    if not parts:
        if start == len(text):
            yield []
        return
    (is_param, value), rest = parts[0], parts[1:]
    if not is_param:
        if text.startswith(value, start):
            yield from _assignments(rest, text, start + len(value))
        return
    if not rest:
        yield [text[start:]]
        return
    following_param, following = rest[0]
    if following_param:  # dos parámetros seguidos: cualquier corte
        ends: Iterator[int] = iter(range(start, len(text) + 1))
    else:
        ends = (match.start() for match in re.finditer(re.escape(following), text[start:]))
        ends = (start + end for end in ends)
    for end in ends:
        for tail in _assignments(rest, text, end):
            yield [text[start:end], *tail]


def _fits(template: Template, text: str) -> Iterator[list[str]]:
    """Cada reparto de `text` que encaja en la plantilla (las cifras, solo en sus parámetros de cifra)."""
    if not all(literal in text for literal in template.literals):
        return
    for count, groups in enumerate(_assignments(template.parts, text)):
        if count >= ASSIGNMENTS_MAX:
            return
        numbers = (group for name, group in zip(template.params, groups, strict=True) if name in NUMBER_PARAMS)
        if all(NUMBER.fullmatch(group) for group in numbers):
            yield groups


def neutral(text: str) -> bool:
    """Sin palabras (números, fechas, horas, «3 MB»): es de cualquier idioma."""
    return not WORD.search(text)


@cache
def belongs(text: str, locale: str) -> bool:
    """¿El texto sale de los catálogos de `locale`? Exacto, o una plantilla suya cuyos parámetros no son de otro idioma;
    si la plantilla solo tiene parámetros, cada uno que lleva un texto (no un dato, `DATA_PARAMS`) debe ser también de
    `locale`, y al menos uno."""
    if neutral(text):
        return True
    found = index(locale)
    if text in found.exact:
        return True
    for template in found.templates:
        for groups in _fits(template, text):
            if any(group == text for group in groups) or any(foreign(group, locale) for group in groups):
                continue
            if template.worded:
                return True
            texts = [g for name, g in zip(template.params, groups, strict=True) if name not in DATA_PARAMS]
            texts = [g for g in texts if not neutral(g)]
            if texts and all(belongs(g, locale) for g in texts):
                return True
    return False


@cache
def known(text: str, locale: str) -> bool:
    """¿Es, sin ambigüedad, un texto de `locale`? Uno exacto de sus catálogos o de su catálogo de mensajes, o una
    plantilla suya con al menos dos palabras propias."""
    found = index(locale)
    if text in found.exact:
        return True
    return any(template.words >= 2 and next(_fits(template, text), None) is not None for template in found.templates)


@cache
def foreign(text: str, locale: str) -> bool:
    """¿Es un texto que solo existe en OTRO idioma (y no en `locale`)?"""
    if neutral(text) or len(text) > DATA_TEXT_MAX:
        return False
    return any(known(text, other) for other in OTHERS[locale]) and not belongs(text, locale)


# ----------------------------------------------------------------------------------------------- el revisor


def _strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for position, item in enumerate(value):
            yield from _strings(item, f"{path}[{position}]")


def leaks(body: dict, locale: str, label: str, evidence: Callable[[str], bool] = lambda _path: False) -> list[str]:
    """Las fugas de idioma de un sobre pedido en `locale` (vacío si no hay). `evidence(ruta_json)`: lo que es
    evidencia guardada tal cual (no se revisa)."""
    found: list[str] = []

    def leak(where: str, text: str, expected: str) -> None:
        found.append(f"{label} [{locale}] {where}: {text!r} no es de {expected}")

    if not belongs(body["message"], locale):
        leak("message", body["message"], locale)
    for position, error in enumerate(body["errors"]):
        if not belongs(error["message"], locale):
            leak(f"errors[{position}].message", error["message"], locale)
    texts = body["i18n"]
    if texts is None:  # una lectura exitosa no lo lleva (regla 3): la app la vuelve a pedir
        if body["statusCode"] >= 400:
            found.append(f"{label} [{locale}] un error sin i18n")
        texts = {locale: {"message": body["message"], "errors": [e["message"] for e in body["errors"]], "texts": []}}
    for each in texts:
        for part in ("errors", "texts"):
            for position, text in enumerate(texts[each][part]):
                if not belongs(text, each):
                    leak(f"i18n.{each}.{part}[{position}]", text, each)
        if not belongs(texts[each]["message"], each):
            leak(f"i18n.{each}.message", texts[each]["message"], each)
    mine = texts[locale]
    if mine["message"] != body["message"] or mine["errors"] != [e["message"] for e in body["errors"]]:
        found.append(f"{label} [{locale}] i18n.{locale} no es igual a message/errors: {mine!r}")
    if len({len(texts[each]["texts"]) for each in texts}) != 1:
        found.append(f"{label} [{locale}] i18n.*.texts con largos distintos: {texts!r}")
    details = [(f"errors[{i}].details", error.get("details")) for i, error in enumerate(body["errors"])]
    for root, value in [("data", body["data"]), *details]:
        for path, text in _strings(value, root):
            if not evidence(path) and foreign(text, locale):
                leak(path, text, locale)
    return found


def check(response, locale: str, label: str) -> list[str]:
    """Las fugas de una respuesta HTTP (lo que no es JSON —una imagen, un archivo— no lleva textos)."""
    if not response.headers.get("content-type", "").startswith("application/json"):
        return []
    if response.headers.get("Content-Language") != locale:
        return [f"{label} [{locale}] Content-Language = {response.headers.get('Content-Language')!r}"]
    body = response.json()
    if "i18n" not in body:
        return [f"{label} [{locale}] sin el sobre de siempre: {sorted(body)}"]
    return leaks(body, locale, label)


def test_the_provenance_index_tells_the_languages_apart():
    """El índice en sí: lo de cada idioma es de ese idioma, lo del otro es ajeno y lo ambiguo no se acusa."""
    assert belongs("Empleado no encontrado", "es-MX") and not belongs("Empleado no encontrado", "en-US")
    assert foreign("Empleado no encontrado", "en-US") and not foreign("Employee not found", "en-US")
    assert belongs("3 sitios", "es-MX") and belongs("3 sites", "en-US") and not belongs("3 sitios", "en-US")
    assert belongs("Peso mexicano", "es-MX") and foreign("Peso mexicano", "en-US")  # un catálogo de la BD
    assert belongs("Mexican peso", "en-US") and foreign("Mexican peso", "es-MX")
    assert belongs("05/10/2026 07:05", "en-US") and not foreign("ZZB Depto", "es-MX")  # sin palabras / un dato
    assert not foreign("Euro", "en-US") and not foreign("Euro", "es-MX")  # igual en los dos idiomas
    # Una plantilla solo de parámetros («{first} {second}») no basta: sus partes deben ser del idioma.
    assert not belongs("Hola mundo", "es-MX")
    # Un parámetro del otro idioma dentro de una plantilla propia es una fuga.
    assert belongs("Foto 2: Rostro no reconocido", "es-MX") and not belongs("Foto 2: Face not recognized", "es-MX")
    assert not belongs("x" * 5 + " " + "Rostro no reconocido", "en-US")


# -------------------------------------------------------------------------------- 1. todas las rutas, dos idiomas

#: El método de cada ruta en el orden en que se llama: primero lo que lee, al final lo que borra (así lo que se
#: elimina no deja sin datos a las lecturas de la misma empresa).
METHOD_ORDER = ("GET", "POST", "PUT", "PATCH", "DELETE")
#: Las cuentas de la empresa B de `tests/test_tenant_isolation.py` (la que tiene datos de todo tipo) y el ADMIN.
ACCOUNTS = {
    "company": ("admin@panificadora.com", "Empresa1234"),
    "employee": ("zzbana@empresa.com", "Empleado123"),
    "validator": ("zzbval@empresa.com", VALIDATOR_PASSWORD),
    "admin": (ADMIN_EMAIL, ADMIN_PASSWORD),
}


def _path_ids(tenants: dict) -> dict[str, object]:  # noqa: F811
    """El id real de cada parámetro de ruta (por su nombre) con los datos de la empresa B."""
    b = tenants["b"]
    with SessionLocal() as db:
        admin_id = db.scalar(select(User.id).where(User.email == ACCOUNTS["company"][0]))
    return {
        **{name: b[name] for name in b if name.endswith("_id")},
        "action": "check-in",
        "company_id": tenants["b_company"],
        "request_id": b["request_id"],
        "session_id": b["work_session_id"],
        "admin_user_id": admin_id,
    }


def _ids_for(path: str, ids: dict[str, object]) -> dict[str, object]:
    """Los ids de una ruta: `user_id` es el administrador de la empresa en las rutas del ADMIN; `session_id`, la
    sesión de la cuenta en `/auth/sessions`."""
    if path.startswith("/admin/companies/{company_id}/admins/"):
        return {**ids, "user_id": ids["admin_user_id"]}
    if path.startswith("/auth/sessions/"):
        return {**ids, "session_id": ids["auth_session_id"]}
    if path.startswith("/me/absences/") or path.endswith(("/approve", "/reject")):
        return {**ids, "absence_id": ids["requested_absence_id"]}  # la que pidió el empleado (pendiente)
    return ids


class Actors:
    """Los encabezados de cada actor; una sesión que una ruta cerró (cerrar sesión, cambiar contraseña) se abre de
    nuevo en la siguiente petición."""

    def __init__(self, client: Any, tenants: dict) -> None:  # noqa: F811
        self.client = client
        self.headers: dict[str, dict[str, str]] = {"key": {"X-API-Key": tenants["b_key"]["secret"]}, "public": {}}

    def of(self, actor: str) -> dict[str, str]:
        if actor not in self.headers:
            self.headers[actor] = login(self.client, *ACCOUNTS[actor])
        return self.headers[actor]

    def answered(self, actor: str, status: int) -> None:
        if status == 401 and actor in ACCOUNTS:
            self.headers.pop(actor, None)


@pytest.mark.parametrize("locale", LOCALES)
def test_every_route_answers_only_in_the_requested_language(client, tenants, locale):  # noqa: F811
    ids, actors = _path_ids(tenants), Actors(client, tenants)
    calls = sorted(routes(), key=lambda item: (ORDER.index(item[0]), METHOD_ORDER.index(item[1]), item[2]))
    problems, reached = [], 0
    for actor, method, path in calls:
        limiter.reset()  # que la petición llegue a su servicio, no al límite
        request = Request(method, path).build(None, "", _ids_for(path, ids))
        headers = {**actors.of(actor), "Accept-Language": locale}
        response = client.request(method, headers=headers, **request)
        actors.answered(actor, response.status_code)
        reached += response.status_code < 400
        problems += check(response, locale, f"{actor} {method} {path} → {response.status_code}")
    assert problems == [], "Fugas de idioma:\n" + "\n".join(problems[:80])
    assert reached > 100  # la matriz llegó a los datos (no todo se quedó en un 404 o en la validación)


# ---------------------------------------------------------------------------------- 2. errores de cada capa


def _down(*_args: object, **_kwargs: object) -> None:
    raise OperationalError("SELECT 1", {}, Exception("BD reiniciándose"))


def _boom(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("detalle interno")


async def _busy(_group: str) -> bool:
    return False


async def _broken(_group: str) -> bool:
    raise RuntimeError("falla dentro de un middleware")


def _patched(target: object, name: str, value: object) -> Callable[[Any, Any], object]:
    """La preparación que reemplaza `target.name` durante la prueba (provoca la falla de una capa)."""
    return lambda monkeypatch, _client: monkeypatch.setattr(target, name, value)


#: (nombre, actor, preparación, método, ruta, argumentos, estado). La preparación provoca la falla de esa capa.
LAYERS: list[tuple[str, str | None, Callable[[Any, Any], object] | None, str, str, dict, int]] = [
    ("400 Content-Length", None, None, "POST", "/api/auth/login", {"headers": {"Content-Length": "x"}}, 400),
    ("401 sin sesión", None, None, "GET", "/api/users/me", {}, 401),
    ("401 token", None, None, "GET", "/api/users/me", {"headers": {"Authorization": "Bearer basura"}}, 401),
    ("401 credenciales", None, None, "POST", "/api/auth/login", {"json": {"email": "a@b.co", "password": "X1x"}}, 401),
    ("401 llave", None, None, "GET", "/api/integrations/v1/company", {}, 401),
    ("401 llave de verificación", None, None, "POST", "/api/integrations/v1/verification/identify", {}, 401),
    ("403 rol", "company", None, "GET", "/api/admin/companies", {}, 403),
    ("404 ruta", None, None, "GET", "/api/no-existe", {}, 404),
    ("404 negocio", "company", None, "GET", "/api/employees/999999", {}, 404),
    ("405", None, None, "PUT", "/api/catalogs", {}, 405),
    ("409", "company", None, "POST", "/api/departments", {"json": {"name": "ventas"}}, 409),
    ("413", None, None, "POST", "/api/face/check", {"headers": {"Content-Length": str(10**9)}}, 413),
    (
        "422 JSON",
        None,
        None,
        "POST",
        "/api/auth/login",
        {"content": b"{no", "headers": {"Content-Type": "application/json"}},
        422,
    ),
    ("422 Pydantic", None, None, "POST", "/api/auth/login", {"json": {}}, 422),
    ("422 regla", "company", None, "POST", "/api/departments", {"json": {"name": "   "}}, 422),
    ("422 control", None, None, "GET", "/api/health/live", {"params": {"q": "a\x00b"}}, 422),
    (
        "429",
        None,
        lambda monkeypatch, client: (
            monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1),
            client.post("/api/auth/login", json={"email": "a@b.co", "password": "X1x"}),
        ),
        "POST",
        "/api/auth/login",
        {"json": {"email": "a@b.co", "password": "X1x"}},
        429,
    ),
    (
        "500 no controlado",
        "company",
        _patched(DepartmentRepository, "search", _boom),
        "GET",
        "/api/departments",
        {},
        500,
    ),
    (
        "500 middleware",
        None,
        _patched(admission, "acquire", _broken),
        "GET",
        "/api/catalogs",
        {},
        500,
    ),
    (
        "503 BD",
        "company",
        _patched(DepartmentRepository, "search", _down),
        "GET",
        "/api/departments",
        {},
        503,
    ),
    (
        "503 saturación",
        None,
        _patched(admission, "acquire", _busy),
        "GET",
        "/api/catalogs",
        {},
        503,
    ),
    (
        "503 salud",
        None,
        _patched(health_service, "readiness", lambda: {"status": "unavailable"}),
        "GET",
        "/api/health/ready",
        {},
        503,
    ),
    ("503 apagado", None, lambda *_: lifecycle.start_draining(), "GET", "/api/health/ready", {}, 503),
]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    ("name", "actor", "prepare", "method", "url", "kwargs", "status"), LAYERS, ids=[layer[0] for layer in LAYERS]
)
def test_errors_of_every_layer_speak_the_requested_language(
    client, company_headers, monkeypatch, locale, name, actor, prepare, method, url, kwargs, status
):
    assert client.post("/api/departments", json={"name": "Ventas"}, headers=company_headers).status_code == 201
    if prepare is not None:
        prepare(monkeypatch, client)
    headers = {**(company_headers if actor else {}), **kwargs.get("headers", {}), "Accept-Language": locale}
    try:
        response = client.request(method, url, **{**kwargs, "headers": headers})
    finally:
        lifecycle.reset()
    assert response.status_code == status, response.text
    assert check(response, locale, name) == []


def test_a_business_error_with_a_rule_text_speaks_one_language(client, company_headers):
    """Un 422 de negocio con el texto de una regla (`LocalizedValueError`: la fecha del RFC no coincide con la de
    nacimiento) y su campo: el sobre lo arma en cada idioma, también con las fechas como se escriben en cada uno."""
    for locale in LOCALES:
        headers = {**company_headers, "Accept-Language": locale}
        employee = create_employee(client, company_headers, number="RFC-1", email="rfc@empresa.com").json()["data"]
        wrong = client.put(f"/api/employees/{employee['id']}", json={"birth_date": "1991-01-01"}, headers=headers)
        assert wrong.status_code == 422, wrong.text
        assert check(wrong, locale, "RFC_BIRTH_DATE_MISMATCH") == []
        client.delete(f"/api/employees/{employee['id']}", headers=company_headers)


# ------------------------------------------------------------------------------------------- 3. el canal en vivo


@pytest.mark.parametrize("locale", LOCALES)
def test_the_live_channel_answers_in_the_language_of_its_url(client, admin_headers, company_headers, locale):
    create_employee(client, company_headers)  # EMP-001 / juan@empresa.com: TAKEN
    problems: list[str] = []
    for headers, messages in (
        (
            company_headers,
            [
                {"type": "validate", "id": "req-00000001", "field": "employee_number", "value": "EMP-001"},
                {"type": "validate", "id": "req-00000002", "field": "employee_number", "value": "EMP-777"},
                {"type": "validate", "id": "req-00000003", "field": "employee_number", "value": "con espacios!"},
                {"type": "validate", "id": "req-00000004", "field": "email", "value": "juan@empresa.com"},
                {"type": "validate", "id": "req-00000005", "field": "email", "value": "no-es-correo"},
                {"type": "validate", "id": "req-00000006", "field": "phone", "value": ""},
                {"type": "validate", "id": "req-00000007", "field": "rfc", "value": ""},
                {"type": "validate", "id": "req-00000008", "field": "department_name", "value": "x" * 101},
                {"type": "validate", "id": "req-00000010", "field": "nss", "value": "123"},
                {"type": "flash", "id": "req-00000011", "token": "basura"},
                {"type": "ping", "id": "req-00000012"},
                {"type": "otro", "id": "req-00000013"},
                {"type": "validate", "id": "req-00000014", "field": "a\x00b", "value": "x"},
            ],
        ),
        (
            admin_headers,
            [
                {"type": "validate", "id": "req-00000021", "field": "company_tax_id", "value": "ABC", "related": "US"},
                {
                    "type": "validate",
                    "id": "req-00000022",
                    "field": "company_tax_id",
                    "value": "PNO120315AB1",
                    "related": "US:MX_RFC",
                },
                {"type": "validate", "id": "req-00000023", "field": "company_admin_email", "value": ""},
                {"type": "validate", "id": "req-00000025", "field": "company_phone", "value": "1"},
                {"type": "validate", "id": "req-00000024", "field": "employee_number", "value": "X"},
            ],
        ),
    ):
        token = headers["Authorization"].removeprefix("Bearer ")
        with client.websocket_connect(f"{WS_URL}?lang={locale}") as socket:
            socket.send_json({"type": "auth", "token": token})
            problems += leaks(socket.receive_json(), locale, "WS auth")
            for message in messages:
                socket.send_json(message)
                answer = socket.receive_json()
                problems += leaks(answer, locale, f"WS {message['type']} {message.get('field', '')}")
                result = answer["data"].get("message") if isinstance(answer["data"], dict) else None
                if result is not None and not belongs(result, locale):
                    problems.append(f"WS {message.get('field')} [{locale}] data.message: {result!r}")
    with client.websocket_connect(f"{WS_URL}?lang={locale}") as socket:  # sin autenticarse
        socket.send_json({"type": "ping"})
        problems += leaks(socket.receive_json(), locale, "WS sin auth")
    assert problems == [], "\n".join(problems)
