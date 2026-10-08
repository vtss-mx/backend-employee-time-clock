"""Contrato único: TODA respuesta de la API (éxito o error, de cualquier capa) tiene la misma forma:
success, statusCode, code, message, data, errors[{code, message, field, details}], i18n, traceId, timestamp.
El frontend depende de eso para procesar cualquier respuesta y avisar de cualquier falla.

`i18n` lleva `message` y `errors[].message` en cada idioma de la API (cambio de idioma en caliente de un aviso abierto,
regla 16): una entrada por idioma de `LOCALES`, en ese orden; la del idioma de la petición es idéntica a `message` y
`errors[].message`, y cada una tiene un texto por error. `texts` (los textos de `data` de lo que no se puede volver a
pedir) tiene el mismo largo en cada idioma y está vacío sin `data`. **Solo donde hace falta** (regla 3 de la raíz): en
todo error (4xx/5xx) y en lo que no se puede volver a pedir (una escritura, el canal en vivo); en una lectura exitosa es
`null` (la app la vuelve a pedir al cambiar el idioma). Su tamaño se mide aquí (`test_the_i18n_block_is_small`). La
PROCEDENCIA de cada texto (que el inglés sea inglés y el español, español) la revisa `tests/test_api_language.py`."""

import pytest
from fastapi.routing import APIRoute

from app.core.config import settings
from app.i18n import LOCALES
from app.main import API_ROUTERS
from tests.conftest import create_employee

KEYS = {"success", "statusCode", "code", "message", "data", "errors", "i18n", "traceId", "timestamp"}
ERROR_KEYS = {"code", "message", "field", "details"}


def assert_texts(body: dict, locale: str) -> None:
    """La forma de `i18n`: cada idioma (en el orden de `LOCALES`) con su `message` y un texto por error; el del idioma
    de la petición, idéntico a los del sobre."""
    texts = body["i18n"]
    assert list(texts) == list(LOCALES), texts
    sizes = set()
    for entry in texts.values():
        assert set(entry) == {"message", "errors", "texts"} and isinstance(entry["message"], str) and entry["message"]
        assert len(entry["errors"]) == len(body["errors"]) and all(isinstance(e, str) and e for e in entry["errors"])
        assert all(isinstance(text, str) and text for text in entry["texts"])
        sizes.add(len(entry["texts"]))
    assert len(sizes) == 1, texts  # el i-ésimo texto de un idioma es el mismo texto en el otro
    assert texts[locale]["message"] == body["message"]
    assert texts[locale]["errors"] == [e["message"] for e in body["errors"]]


def assert_envelope(response, status: int, code: str | None = None, locale: str = "es-MX") -> dict:
    body = response.json()
    # El sobre dice en qué idioma va y que depende de Accept-Language (regla 16): ningún caché mezcla idiomas.
    assert response.headers["Content-Language"] == locale and "Accept-Language" in response.headers["Vary"]
    assert set(body) == KEYS, f"{response.request.method} {response.request.url}: {sorted(body)}"
    assert body["statusCode"] == response.status_code == status
    assert body["success"] is (status < 400)
    assert isinstance(body["message"], str) and body["message"]
    assert body["traceId"] == response.headers["X-Request-ID"]
    if status < 400 and response.request.method == "GET":
        assert body["i18n"] is None  # una lectura exitosa se vuelve a pedir al cambiar el idioma: no lo lleva
        return _checked(body, status, code)
    assert_texts(body, locale)
    if body["data"] is None:
        assert all(entry["texts"] == [] for entry in body["i18n"].values())
    return _checked(body, status, code)


def _checked(body: dict, status: int, code: str | None) -> dict:
    if status >= 400:
        assert body["errors"] and all(set(e) == ERROR_KEYS for e in body["errors"]), body["errors"]
    if code:
        assert body["code"] == code
    return body


def test_every_route_declares_the_envelope():
    """Las respuestas exitosas de cada ruta se documentan y validan como ApiResponse[...]."""
    missing = [
        f"{sorted(route.methods)} {route.path}"
        for router in API_ROUTERS
        for route in router.routes
        if isinstance(route, APIRoute) and not getattr(route.response_model, "__name__", "").startswith("ApiResponse")
    ]
    assert not missing


def test_success_has_the_envelope(client, company_headers):
    assert_envelope(client.get("/api/users/me", headers=company_headers), 200)
    assert_envelope(client.get("/api/health/live"), 200, "ALIVE")
    english = {**company_headers, "Accept-Language": "en-US"}
    assert assert_envelope(client.get("/api/users/me", headers=english), 200, locale="en-US")["message"] == (
        "Authenticated user"
    )


@pytest.mark.parametrize(
    ("method", "url", "kwargs", "status", "code"),
    [
        ("GET", "/api/no-existe", {}, 404, None),  # ruta inexistente
        ("PUT", "/api/catalogs", {}, 405, None),  # método no permitido
        ("GET", "/api/users/me", {}, 401, None),  # sin sesión
        ("GET", "/api/users/me", {"headers": {"Authorization": "Bearer basura"}}, 401, None),  # token inválido
        (
            "POST",
            "/api/auth/login",
            {"content": b"{no-es-json", "headers": {"Content-Type": "application/json"}},
            422,
            None,
        ),
        ("POST", "/api/auth/login", {"json": {"email": "no-es-correo"}}, 422, "VALIDATION_ERROR"),
        ("POST", "/api/auth/login", {"json": {"email": "nadie@empresa.com", "password": "Equivocada1"}}, 401, None),
        ("POST", "/api/face/check", {"headers": {"Content-Length": str(10**9)}}, 413, "PAYLOAD_TOO_LARGE"),
        ("POST", "/api/telemetry/web", {"headers": {"Content-Length": str(10**6)}}, 413, "PAYLOAD_TOO_LARGE"),
        ("POST", "/api/telemetry/web", {"json": {"samples": []}}, 422, "VALIDATION_ERROR"),
        ("GET", "/api/integrations/v1/company", {}, 401, None),  # API de integración sin llave
        # API pública de verificación (SDK móviles): sin llave y con una llave inventada.
        ("POST", "/api/integrations/v1/verification/challenge", {}, 401, "API_KEY_REQUIRED"),
        (
            "POST",
            "/api/integrations/v1/verification/verify",
            {"headers": {"X-API-Key": "tck_x"}},
            401,
            "API_KEY_INVALID",
        ),
    ],
)
@pytest.mark.parametrize("locale", ["es-MX", "en-US"])
def test_errors_from_every_layer_have_the_envelope(client, method, url, kwargs, status, code, locale):
    """El mismo contrato en los dos idiomas: solo cambian los textos (`message`, `errors[].message`), nunca los
    códigos."""
    headers = {**kwargs.get("headers", {}), "Accept-Language": locale}
    body = assert_envelope(client.request(method, url, **{**kwargs, "headers": headers}), status, code, locale)
    assert all(error["message"] for error in body["errors"])


def test_forbidden_and_business_errors_have_the_envelope(client, company_headers, admin_headers):
    assert_envelope(client.get("/api/admin/companies", headers=company_headers), 403)  # rol sin permiso
    english = {**company_headers, "Accept-Language": "en-US"}
    forbidden = assert_envelope(client.get("/api/admin/companies", headers=english), 403, locale="en-US")
    assert forbidden["message"] == "You don't have permission for this action"
    assert_envelope(client.get("/api/employees/999999", headers=company_headers), 404, "EMPLOYEE_NOT_FOUND")
    taken = client.post("/api/departments", json={"name": "A"}, headers=company_headers)
    assert_envelope(taken, 201)
    assert_envelope(client.post("/api/departments", json={"name": "a"}, headers=company_headers), 409)
    body = assert_envelope(client.post("/api/departments", json={"name": ""}, headers=company_headers), 422)
    assert body["errors"][0]["field"] == "name"


def test_a_mutation_carries_the_texts_of_its_data_in_every_language(client, company_headers):
    """Lo que no se puede volver a pedir (un POST) lleva los textos de su `data` en cada idioma (`texts`, mismo orden):
    la aplicación web cambia de idioma el motivo que el sistema guardó sin repetir la petición. Una lectura, no."""
    employee = create_employee(client, company_headers).json()["data"]
    english = {**company_headers, "Accept-Language": "en-US"}
    body = assert_envelope(
        client.post(f"/api/employees/{employee['id']}/face/reset", headers=english), 200, locale="en-US"
    )
    reason = body["data"]["face_rejection_reason"]
    position = body["i18n"]["en-US"]["texts"].index(reason)
    assert reason == "Your company asked you to verify your identity again"
    assert body["i18n"]["es-MX"]["texts"][position] == "Tu empresa pidió que verifiques tu identidad de nuevo"
    read = assert_envelope(client.get(f"/api/employees/{employee['id']}", headers=english), 200, locale="en-US")
    assert read["data"]["face_rejection_reason"] == reason and read["i18n"] is None


def test_the_i18n_block_is_small(client, company_headers):
    """Tamaño de `i18n` (regla 17: en MB): solo va en errores y escrituras, y crece con el número de idiomas. Con los
    siete idiomas (es-MX, en-US, pt-BR, fr-FR, de-DE, it-IT, es-ES), un error de validación con 3 campos lleva
    0.000931 MB (0.000133 MB por idioma; medido el 2026-10-06), y ninguna lectura exitosa (listados y detalles: casi
    todo el tráfico) lo lleva."""
    import json

    invalid = client.post("/api/departments", json={"name": "", "description": 7, "extra": []}, headers=company_headers)
    body = invalid.json()
    size = len(json.dumps(body["i18n"], ensure_ascii=False).encode()) / (1024 * 1024)
    per_locale = size / len(LOCALES)
    assert len(LOCALES) == 7 and size < 0.0015 and per_locale < 0.0003
    assert client.get("/api/departments", headers=company_headers).json()["i18n"] is None


def test_rate_limit_has_the_envelope(client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1)
    payload = {"email": "admin@empresa.com", "password": "Equivocada1"}
    client.post("/api/auth/login", json=payload)
    response = client.post("/api/auth/login", json=payload, headers={"Accept-Language": "en-US"})
    assert assert_envelope(response, 429, locale="en-US")["message"] == "Too many requests. Try again in a few seconds."
    assert response.headers.get("Retry-After")


def test_unexpected_errors_have_the_envelope_without_leaking_details(client, company_headers, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("detalle interno que el cliente no debe ver")

    from app.routers import catalogs

    monkeypatch.setattr(catalogs, "get_catalogs", boom)
    response = client.get("/api/catalogs", headers=company_headers)
    body = assert_envelope(response, 500, "INTERNAL_ERROR")
    assert "detalle interno" not in response.text and body["data"] is None
