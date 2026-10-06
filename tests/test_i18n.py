"""Idiomas de la API (regla 16 de la raíz): es-MX (por omisión) y en-US.

- Negociación: `Accept-Language` (pesos `q`, `es*` → es-MX, `en*` → en-US, lo demás o nada → es-MX), `?lang=` en el
  canal en vivo, `Content-Language` y `Vary: Accept-Language` en cada respuesta JSON, sin consultas de más.
- Catálogo de mensajes: los dos idiomas con las mismas llaves por área, los mismos `{parámetros}` y las mismas formas
  de plural; cada llave que el código nombra existe; ningún texto escrito a mano en un error ni en `ok(...)`.
- Catálogos de la BD: cada texto de cada registro tiene su traducción (y ninguna sobra), con sus mismos marcadores y
  dentro del largo de su columna; la API los entrega en el idioma de la petición (también el menú de `/users/me`).
- Textos que se guardan (`stored`): se traducen al leerse, en el idioma de quien lee.
"""

import ast
import json
import string
import time
from datetime import date, datetime
from datetime import time as clock
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core import validation_errors
from app.core.database import SessionLocal
from app.core.exceptions import (
    _HTTP_CODES,
    AppError,
    ConflictError,
    NotFoundError,
    register_exception_handlers,
)
from app.core.responses import ok
from app.i18n import (
    DEFAULT_LOCALE,
    LOCALES,
    DayMonth,
    LocalizedValueError,
    Megabytes,
    MissingTextError,
    Text,
    current_locale,
    format_list,
    has_text,
    locale_of,
    negotiate,
    read_stored,
    stored,
    strict,
    t,
    use_locale,
)
from app.i18n.messages import MESSAGES, en_us, es_mx
from app.i18n.messages.base import merge
from app.models import CatalogTranslation
from app.models.catalog import TRANSLATED_FIELDS, TRANSLATION_LOCALES
from app.models.catalog_seed import load_catalog_seed, load_translation_seed
from app.services import availability_service, face_security, health_service
from app.services.attendance_service import ACTION_DONE
from app.services.calendar_rules import official_holidays
from app.services.catalog_service import ALPHABETICAL, CATALOG_MODELS, get_catalogs
from app.services.policy_governance import CATALOG_FIELDS
from app.services.shift_rules import WEEKDAY_NAMES
from tests.conftest import create_employee, login

ROOT = Path(__file__).resolve().parents[1]
EN = {"Accept-Language": "en-US"}
FORMS = {"zero", "one", "other"}
#: Las clases de error de la API (su texto sale del catálogo de mensajes).
APP_ERRORS = {
    "AppError",
    "AuthenticationError",
    "ApiKeyAuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableError",
    "PayloadTooLargeError",
    "ServiceUnavailableError",
    "RateLimitError",
}
#: Funciones que reciben una llave de mensaje como primer argumento.
KEY_CALLS = {"t", "Text", "LocalizedValueError", "stored", "company_suspended", "_required"}


# ---------------------------------------------------------------- negociación


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "es-MX"),
        ("", "es-MX"),
        ("en-US", "en-US"),
        ("en", "en-US"),
        ("EN-gb", "en-US"),
        ("en_US", "en-US"),
        ("es-ES", "es-MX"),
        ("es-419,en;q=0.5", "es-MX"),
        ("fr-FR,en;q=0.8,es;q=0.9", "es-MX"),  # el de mayor peso que la API habla
        ("fr-FR,en;q=0.8", "en-US"),
        ("en;q=0.7,es;q=0.7", "en-US"),  # mismo peso: el primero
        ("en;q=0,es;q=0.1", "es-MX"),  # q=0 lo excluye
        ("en;q=0", "es-MX"),
        ("en;q=abc,es;q=0.2", "es-MX"),  # un peso mal escrito descarta el rango
        ("en;q=2", "es-MX"),  # fuera de 0-1
        ("en;level=1;q=0.9", "en-US"),  # otros parámetros no estorban
        ("fr, de, *", "es-MX"),  # nada que la API hable
        ("en;foo=bar", "en-US"),  # sin q: peso 1
    ],
)
def test_negotiation_follows_rfc_9110(header, expected):
    assert negotiate(header) == expected


def test_a_huge_header_reads_only_its_first_ranges():
    assert negotiate(",".join(["fr"] * 16 + ["en"])) == "es-MX"
    assert negotiate(",".join(["fr"] * 15 + ["en"])) == "en-US"


def test_locale_of_a_tag():
    assert locale_of(None) is None and locale_of("") is None and locale_of("pt-BR") is None
    assert locale_of(" es ") == "es-MX"


def test_negotiation_costs_microseconds():
    """Corre en cada petición: sin consultas y en microsegundos (medido: ≈1 µs por cabecera típica)."""
    header = "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7"
    started = time.perf_counter()
    for _ in range(10_000):
        negotiate(header)
    per_call = (time.perf_counter() - started) / 10_000
    assert per_call < 50e-6, per_call


def test_outside_a_request_the_default_locale_applies():
    assert current_locale() == DEFAULT_LOCALE == LOCALES[0]
    with use_locale("en-US"):
        assert current_locale() == "en-US"
    assert current_locale() == "es-MX"


def test_every_json_response_says_its_language_and_that_it_varies(client, company_headers):
    spanish = client.get("/api/health/live")
    assert spanish.headers["Content-Language"] == "es-MX" and "Accept-Language" in spanish.headers["Vary"]
    assert spanish.json()["message"] == "El servicio está en ejecución"
    english = client.get("/api/health/live", headers=EN)
    assert english.headers["Content-Language"] == "en-US" and english.json()["message"] == "The service is running"
    unknown = client.get("/api/health/live", headers={"Accept-Language": "fr-FR"})
    assert unknown.headers["Content-Language"] == "es-MX" and unknown.json()["message"] == spanish.json()["message"]
    # CORS agrega su Vary después: se suman, no se pisan.
    cors = client.get("/api/health/live", headers={**EN, "Origin": "http://localhost:5173"})
    assert {"Accept-Language", "Origin"} <= {part.strip() for part in cors.headers["Vary"].split(",")}
    # Lo que no es JSON (la documentación, una imagen) no cambia con el idioma.
    docs = client.get("/docs", headers=EN)
    assert docs.status_code == 200 and "Content-Language" not in docs.headers


def test_errors_of_every_layer_come_in_the_language_of_the_request(client, company_headers):
    missing = client.get("/api/no-existe", headers=EN)
    assert missing.json()["message"] == "The requested resource doesn't exist"
    assert client.put("/api/catalogs", headers=EN).json()["message"] == "HTTP method not allowed for this resource"
    assert client.get("/api/users/me", headers=EN).json()["message"] == "Sign in to continue"
    too_large = client.post("/api/face/check", headers={**EN, "Content-Length": str(10**9)})
    assert too_large.json()["message"].startswith("The request exceeds the maximum size (")
    not_found = client.get("/api/employees/999999", headers={**company_headers, **EN})
    assert not_found.json()["message"] == "Employee not found"
    bad_json = client.post("/api/auth/login", content=b"{no", headers={**EN, "Content-Type": "application/json"})
    assert bad_json.json()["errors"][0]["message"] == "The content isn't valid JSON"
    wrong = client.post("/api/auth/login", json={"email": "nadie@empresa.com", "password": "Equivocada1"}, headers=EN)
    assert wrong.json()["message"] == "Incorrect email or password"


# ---------------------------------------------------------------- validación (422)


def test_pydantic_messages_come_in_the_language_of_the_request(client, company_headers):
    body = {"name": ""}
    spanish = client.post("/api/departments", json=body, headers=company_headers).json()
    english = client.post("/api/departments", json=body, headers={**company_headers, **EN}).json()
    assert spanish["errors"][0]["code"] == english["errors"][0]["code"]  # el código no cambia
    assert spanish["errors"][0]["message"] == "Escribe al menos 1 carácter"  # antes, el inglés de Pydantic
    assert english["errors"][0]["message"] == "Enter at least 1 character"
    blank = client.post("/api/departments", json={"name": "   "}, headers={**company_headers, **EN}).json()
    assert blank["errors"][0]["code"] == "VALUE_ERROR" and blank["errors"][0]["message"] == "Name is required"
    missing = client.post("/api/auth/login", json={}, headers=EN).json()
    assert missing["message"] == "The submitted data isn't valid"
    assert {e["message"] for e in missing["errors"]} == {"This field is required"}
    short = client.post("/api/auth/login", json={"email": "a@b.co", "password": ""}, headers=EN).json()
    assert short["errors"][0]["code"] == "STRING_TOO_SHORT"
    assert short["errors"][0]["message"] == "Enter at least 1 character"


def test_every_pydantic_error_type_has_its_message_in_both_languages():
    for key, datum in validation_errors._BY_TYPE.values():
        for locale in LOCALES:
            params = {"count": 3, "limit": 3} if datum else None
            assert t(key, params, locale=locale) != key


def test_a_validation_error_without_its_limit_or_of_an_unknown_type_still_says_something():
    """Sin el dato que su mensaje necesita (o de un tipo nuevo de Pydantic): el genérico, nunca el inglés de
    Pydantic."""
    assert validation_errors.message_of({"type": "string_too_short"}) == "El valor no es válido"
    with use_locale("en-US"):
        assert validation_errors.message_of({"type": "greater_than", "ctx": {}}) == "The value isn't valid"
        assert validation_errors.message_of({"type": "a_new_pydantic_type"}) == "The value isn't valid"
        assert validation_errors.message_of({"type": "greater_than", "ctx": {"gt": date(2026, 1, 2)}}) == (
            "Must be greater than 01/02/2026"
        )
        error = LocalizedValueError("NAME_REQUIRED")
        assert validation_errors.message_of({"type": "value_error", "ctx": {"error": error}}) == "Name is required"
        assert str(error) == "Name is required"


# ---------------------------------------------------------------- el catálogo de mensajes


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_both_languages_have_the_same_areas_keys_placeholders_and_plural_forms():
    assert es_mx.AREAS and [a.__name__.rsplit(".", 1)[-1] for a in es_mx.AREAS] == [
        a.__name__.rsplit(".", 1)[-1] for a in en_us.AREAS
    ]
    for spanish, english in zip(es_mx.AREAS, en_us.AREAS, strict=True):
        assert spanish.MESSAGES.keys() == english.MESSAGES.keys(), spanish.__name__
        for key, es_text in spanish.MESSAGES.items():
            en_text = english.MESSAGES[key]
            assert isinstance(es_text, str) == isinstance(en_text, str), key
            if isinstance(es_text, str):
                assert _fields(es_text) == _fields(str(en_text)), key
                continue
            assert isinstance(en_text, dict) and es_text.keys() == en_text.keys() <= FORMS, key
            assert {"one", "other"} <= es_text.keys(), key
            for form, text in es_text.items():
                assert _fields(text) == _fields(en_text[form]), (key, form)


def test_a_key_repeated_in_two_areas_is_an_error():
    with pytest.raises(ValueError, match="repetidas"):
        merge({"A": "uno"}, {"A": "otro"})


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name:
                yield name, node


def _literals(node: ast.expr | None) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _literals(node.body) + _literals(node.orelse)
    return []


def _app_sources():
    for path in sorted((ROOT / "app").rglob("*.py")):
        if "i18n/messages" not in str(path):
            yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_no_error_or_response_is_written_by_hand():
    """El texto de un error o de una respuesta sale del catálogo: nunca una cadena (ni f-string) en `raise
    XError("...")` ni en `ok(data, "...")`; ni un `ValueError("...")` en las reglas de los esquemas."""
    written = []
    for path, tree in _app_sources():
        for name, node in _calls(tree):
            position = 1 if name == "ok" else 0
            if name in APP_ERRORS or name == "ok":
                message = node.args[position] if len(node.args) > position else None
                if isinstance(message, ast.JoinedStr) or _literals(message):
                    written.append(f"{path.name}:{node.lineno}")
            if name == "ValueError" and "schemas" in path.parts and (node.args and _literals(node.args[0])):
                written.append(f"{path.name}:{node.lineno}")
    assert not written


def _referenced_keys() -> dict[str, str]:
    """Cada llave que el código nombra literalmente: `t("…")`, `Text("…")`, `key="…"` y el `code="…"` de un error
    o de `ok(...)` sin texto propio."""
    keys: dict[str, str] = {}
    for path, tree in _app_sources():
        for name, node in _calls(tree):
            where = f"{path.name}:{node.lineno}"
            keywords = {k.arg: k.value for k in node.keywords}
            if name in APP_ERRORS or name == "ok":
                position = 1 if name == "ok" else 0
                has_message = len(node.args) > position or "message" in keywords
                named = keywords.get("key") or (None if has_message else keywords.get("code"))
                for key in _literals(named):
                    keys.setdefault(key, where)
            elif name in KEY_CALLS and node.args:
                for key in _literals(node.args[0]):
                    keys.setdefault(key, where)
    return keys


def _dynamic_keys() -> set[str]:
    """Las llaves que el código guarda en tablas (las elige al correr)."""
    return {
        *ACTION_DONE.values(),
        *health_service.MESSAGES.values(),
        *WEEKDAY_NAMES,
        *(key for _, key in official_holidays(2024)),
        *availability_service._AVAILABLE.values(),
        *availability_service._TAKEN.values(),
        *availability_service._EMPTY.values(),
        *(key for key, _ in validation_errors._BY_TYPE.values()),
        *_HTTP_CODES.values(),
        "HTTP_ERROR",
        *(code for _, _, code in CATALOG_FIELDS),
        *(signal.name for signal in face_security.SIGNALS),
    }


def test_every_key_the_code_names_has_its_message():
    missing = {key: where for key, where in _referenced_keys().items() if key not in MESSAGES["es-MX"]}
    assert not missing
    assert not _dynamic_keys() - MESSAGES["es-MX"].keys()


def test_every_message_is_named_somewhere():
    """Un mensaje que nadie usa sobra (se borra con el código que lo usaba)."""
    used = set(_referenced_keys()) | _dynamic_keys()
    sources = "\n".join(path.read_text(encoding="utf-8") for path, _ in _app_sources())
    unused = [key for key in MESSAGES["es-MX"] if key not in used and f'"{key}"' not in sources]
    assert not unused


# ---------------------------------------------------------------- armar un texto


def test_plurals_counts_and_formats_in_both_languages():
    assert t("SITES", {"count": 1}) == "1 sitio" and t("SITES", {"count": 1_234}) == "1,234 sitios"
    assert t("SITES", {"count": 0}) == "0 sitios"
    assert t("PASSWORD_CHANGED", {"count": 0}) == "Contraseña actualizada."  # forma `zero`
    with use_locale("en-US"):
        assert t("SITES", {"count": 1}) == "1 site" and t("SITES", {"count": 2}) == "2 sites"
        assert t("NEXT_SHIFT", {"day": DayMonth(date(2026, 10, 5)), "start": clock(14, 5), "opens": clock(0, 30)}) == (
            "Your next shift is on 10/05 at 2:05 PM; you can check in from 12:30 AM."
        )
        assert format_list(["A"]) == "A" and format_list(["A", "B"]) == "A and B"
        assert format_list(["A", "B", Text("MONDAY")]) == "A, B, and Monday"
        assert t("REQUEST_TOO_LARGE", {"size": Megabytes(5 * 1024 * 1024)}) == (
            "The request exceeds the maximum size (5.00 MB)"
        )
    assert t("NEXT_SHIFT", {"day": DayMonth(date(2026, 10, 5)), "start": clock(14, 5), "opens": clock(9, 0)}) == (
        "Tu siguiente turno es el 05/10 a las 14:05; puedes checar desde las 09:00."
    )
    assert format_list(["A", "B", "C"]) == "A, B y C"
    assert t("REQUEST_TOO_LARGE", {"size": Megabytes(1024)}) == "La solicitud excede el tamaño máximo (< 0.01 MB)"
    assert t("SENTENCE", {"text": datetime(2026, 10, 5, 7, 5)}) == "05/10/2026 07:05."
    assert t("SENTENCE", {"text": 3.5}) == "3.5."


def test_a_missing_key_or_parameter_never_breaks_a_response_in_production(caplog):
    strict(False)
    try:
        assert t("NO_EXISTE") == "NO_EXISTE"
        assert t("SITES", {}) == "{count} sitios"
    finally:
        strict(True)
    assert "Texto sin traducción" in caplog.text
    with pytest.raises(MissingTextError):
        t("NO_EXISTE")
    assert not has_text("NO_EXISTE") and has_text("SITES")


def test_an_error_renders_its_message_when_it_is_read():
    error = ConflictError(code="SITE_IN_USE", params={"count": 2, "shifts": ["A", "B"]})
    assert (
        str(error) == "El sitio está en los turnos A y B: quítalo de esos turnos o desactívalo en lugar de eliminarlo"
    )
    with use_locale("en-US"):
        assert error.message == (
            "The site is used by the A and B shifts: remove it from those shifts or deactivate it instead of "
            "deleting it"
        )
    assert NotFoundError().message == "El recurso solicitado no existe"  # el mensaje de su código por omisión
    assert AppError("Texto ya traducido", code="X").message == "Texto ya traducido"


def test_ok_uses_the_message_of_its_code():
    with use_locale("en-US"):
        assert ok(None, code="SITES", params={"count": 3}).message == "3 sites"
        assert ok(None).message == "Done"


def test_the_handlers_answer_in_the_language_of_the_request():
    """Un `HTTPException` de un estado poco común (418) responde el genérico del catálogo."""
    from starlette.exceptions import HTTPException

    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/teapot")
    def teapot() -> None:
        raise HTTPException(status_code=418)

    with TestClient(probe) as raw:
        assert raw.get("/teapot").json()["message"] == "Solicitud no procesada"


# ---------------------------------------------------------------- textos guardados


def test_a_stored_text_is_read_in_the_language_of_the_reader():
    note = stored("SUSPENSION_NON_PAYMENT_NOTE", {"due_on": date(2026, 10, 1), "count": 5})
    assert note.startswith("i18n:") and len(note) <= 300
    assert read_stored(note) == "Cargo vencido el 01/10/2026 sin pagar después de 5 días de gracia."
    with use_locale("en-US"):
        assert read_stored(note) == "Charge due on 10/01/2026 still unpaid after 5 grace days."
    # Lo que escribió una persona, lo guardado antes de esta forma o algo que ya no se reconoce: tal cual.
    for value in (None, "Lo escribió el ADMIN", "i18n:{no json", 'i18n:{"sin": "llave"}', 'i18n:["lista"]'):
        assert read_stored(value) == value
    unknown = stored("YA_NO_EXISTE")
    assert read_stored(unknown) == unknown


def test_the_default_reverification_reason_is_read_in_each_language(client, company_headers):
    employee = create_employee(client, company_headers).json()["data"]
    assert client.post(f"/api/employees/{employee['id']}/face/reset", headers=company_headers).status_code == 200
    spanish = client.get(f"/api/employees/{employee['id']}", headers=company_headers).json()["data"]
    english = client.get(f"/api/employees/{employee['id']}", headers={**company_headers, **EN}).json()["data"]
    assert spanish["face_rejection_reason"] == "Tu empresa pidió que verifiques tu identidad de nuevo"
    assert english["face_rejection_reason"] == "Your company asked you to verify your identity again"
    own = client.get("/api/users/me", headers={**login(client, "juan@empresa.com", "Empleado123"), **EN})
    assert own.json()["data"]["employee"]["face_rejection_reason"] == english["face_rejection_reason"]


# ---------------------------------------------------------------- catálogos de la BD


def _texts(row: dict) -> dict[str, str]:
    return {field: row[field] for field in TRANSLATED_FIELDS if row.get(field)}


def test_every_catalog_text_has_its_translation_and_none_is_left_over():
    seed, translations = load_catalog_seed(), load_translation_seed()
    assert set(translations) == set(TRANSLATION_LOCALES)
    for locale, catalogs in translations.items():
        expected = {
            table: {row["code"]: _texts(row) for row in rows if "code" in row and _texts(row)}
            for table, rows in seed.items()
        }
        expected = {table: codes for table, codes in expected.items() if codes}
        assert set(catalogs) == set(expected), locale
        for table, codes in expected.items():
            assert set(catalogs[table]) == set(codes), (locale, table)
            columns = CATALOG_MODELS[table].__table__.columns if table in CATALOG_MODELS else None
            for code, fields in codes.items():
                translated = catalogs[table][code]
                assert set(translated) == set(fields), (locale, table, code)
                for field, text in translated.items():
                    assert text.strip() == text and text, (table, code, field)
                    assert _fields(text) == _fields(fields[field]), (table, code, field)
                    if columns is not None:
                        assert len(text) <= columns[field].type.length, (table, code, field)


def test_the_translations_are_in_the_database_and_the_catalogs_use_them(client, company_headers):
    with SessionLocal() as db:
        stored_rows = db.scalar(select(func.count()).select_from(CatalogTranslation))
    seeded = sum(len(fields) for codes in load_translation_seed()["en-US"].values() for fields in codes.values())
    assert stored_rows == seeded
    with use_locale("en-US"):
        catalogs = get_catalogs()
        assert catalogs.locale == "en-US" and catalogs.name("currencies", "MXN") == "Mexican peso"
        assert catalogs.reason_message("NO_MATCH") == "Face not recognized"
        assert catalogs.accessories_message(["GLASSES", "MASK"]) == "Remove your glasses and your face mask to continue"
        assert catalogs.liveness_instruction("TURN_LEFT") == "Slowly turn your head to your left"
    assert get_catalogs().name("currencies", "MXN") == "Peso mexicano"
    assert (
        get_catalogs().accessories_message(["GLASSES", "MASK"]) == "Quítate los lentes y el cubrebocas para continuar"
    )


def test_the_catalogs_endpoint_answers_in_english(client, company_headers):
    response = client.get("/api/catalogs", headers={**company_headers, **EN})
    data = response.json()["data"]
    assert response.json()["message"] == "Catalogs" and response.headers["Content-Language"] == "en-US"
    assert {c["code"]: c["name"] for c in data["currencies"]} == {
        "MXN": "Mexican peso",
        "USD": "US dollar",
        "EUR": "Euro",
    }
    assert {d["code"]: d["phrase"] for d in data["day_off_types"]}["VACATION"] == "You're on vacation"
    assert {f["code"]: f["name"] for f in data["flash_modes"]}["OBSERVE"] == "Measure only"
    # Los países, en orden alfabético en cada idioma (los destacados primero) y renumerados.
    countries = data["countries"]
    featured = [c["code"] for c in countries if c["featured"]]
    rest = [c["name"] for c in countries if not c["featured"]]
    assert featured[0] == "MX" and rest[0] == "Afghanistan" and rest[1] == "Åland Islands"
    assert [c["sort_order"] for c in countries] == list(range(1, len(countries) + 1))
    spanish = client.get("/api/catalogs", headers=company_headers).json()["data"]
    assert {c["code"]: c["name"] for c in spanish["currencies"]}["MXN"] == "Peso mexicano"
    # Los roles, con su nombre en cada idioma (migración 0071: el español ya no los muestra en inglés).
    roles = ("ADMIN", "COMPANY", "EMPLOYEE", "VALIDATOR")
    assert [{r["code"]: r["name"] for r in data["roles"]}[code] for code in roles] == [
        "Admin",
        "Company",
        "Employee",
        "Validator",
    ]
    assert [{r["code"]: r["name"] for r in spanish["roles"]}[code] for code in roles] == [
        "Administrador",
        "Empresa",
        "Empleado",
        "Validador",
    ]
    assert {"countries"} == ALPHABETICAL


def test_the_menu_comes_in_the_language_of_the_request(client, company_headers):
    english = client.get("/api/users/me", headers={**company_headers, **EN}).json()["data"]
    names = {screen["code"]: screen["name"] for screen in english["screens"]}
    assert names["COMPANY_DASHBOARD"] == "Dashboard" and names["COMPANY_SITES"] == "Work sites"
    assert {m["code"]: m["name"] for m in english["modules"]}["PEOPLE"] == "Staff"
    spanish = client.get("/api/users/me", headers=company_headers).json()["data"]
    spanish_names = {screen["code"]: screen["name"] for screen in spanish["screens"]}
    # El inicio de la empresa se llama como el del ADMIN: «Panel» en español, "Dashboard" en inglés (migración 0071).
    assert spanish_names["COMPANY_DASHBOARD"] == "Panel" and spanish_names["COMPANY_SITES"] == "Sitios de trabajo"


def test_the_seed_files_are_valid_utf8_json():
    for path in sorted((ROOT / "alembic" / "seed").glob("catalogs*.json")):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path
