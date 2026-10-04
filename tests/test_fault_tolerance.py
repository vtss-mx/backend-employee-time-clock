"""Tolerancia a fallas: cada prueba provoca una falla real y verifica que el sistema la soporte
(responde con su código y el sobre de siempre, sigue atendiendo lo demás y no pierde datos)."""

import json
from typing import Annotated

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Form, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core import crypto
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import ServiceUnavailableError, register_exception_handlers
from app.middleware.security import SecurityMiddleware
from app.models import FaceEmbedding
from app.services import catalog_service, maintenance_service
from app.services.face_gallery import FaceGalleryCache, face_galleries
from tests.conftest import approved_employee, create_employee, login, turn_files
from tests.test_validators import approved, identify_face, validator_headers

COOKIE = settings.REFRESH_COOKIE_NAME


@pytest.fixture(autouse=True)
def _fresh_gallery():
    face_galleries.clear()
    yield


def _verify(client, headers, *, turn: str = "turn:{person}"):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"c{i}.jpg", b"face:juan", "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "juan", image=turn)
    data = {"challenge_id": challenge["challenge_id"]}
    return client.post("/api/verification/face", data=data, files=files, headers=headers)


def _corrupt_sample(employee_id: int) -> None:
    with SessionLocal() as db:
        row = db.query(FaceEmbedding).filter_by(employee_id=employee_id).order_by(FaceEmbedding.id).first()
        row.embedding_encrypted = b"dato-danado"
        db.commit()


# ---------------------------------------------------------------- datos cifrados


def test_unreadable_samples_are_skipped_not_fatal(client, company_headers):
    juan = approved(client, company_headers, "juan", number="EMP-001")
    approved(client, company_headers, "ana", number="EMP-002")
    _corrupt_sample(juan["id"])  # una de sus 3 muestras quedó ilegible
    headers = validator_headers(client, company_headers, mode="FACE")
    found = identify_face(client, headers, "juan").json()["data"]  # la galería se arma sin ella
    assert found["verified"] is True and found["employee_id"] == juan["id"]
    employee = login(client, "juan@empresa.com", "Empleado123")
    assert _verify(client, employee).json()["data"]["verified"] is True  # 1:1 con las muestras legibles


def test_previous_encryption_keys_still_read_old_data(monkeypatch):
    old = Fernet.generate_key()
    stored = Fernet(old).encrypt(b"vector")
    assert crypto.try_decrypt(stored) is None  # con solo la llave nueva no se lee
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_PREVIOUS_KEYS", old.decode())
    monkeypatch.setattr(
        crypto, "_fernet", crypto.MultiFernet([Fernet(k.encode()) for k in settings.data_encryption_keys])
    )
    assert crypto.decrypt_bytes(stored) == b"vector"
    assert crypto.decrypt_bytes(crypto.encrypt_bytes(b"nuevo")) == b"nuevo"
    with pytest.raises(ValueError):
        crypto.decrypt_bytes(b"basura")


# ---------------------------------------------------------------- sesión


def test_refresh_retry_after_a_lost_response_gets_the_same_token(client):
    login(client, "admin@empresa.com", "Admin1234")
    first = client.cookies.get(COOKIE)
    assert client.post("/api/auth/refresh").status_code == 200
    second = client.cookies.get(COOKIE)
    # La respuesta se perdió: el teléfono reintenta con el token anterior (dentro de la gracia).
    client.cookies.set(COOKIE, first, path="/api/auth")
    retry = client.post("/api/auth/refresh")
    assert retry.status_code == 200 and retry.cookies.get(COOKIE) == second  # el mismo, idempotente
    client.cookies.clear()
    client.cookies.set(COOKIE, second, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 200  # y con él la sesión sigue sana


def test_remembering_the_account_never_breaks_a_login(client, monkeypatch):
    from app.services.remembered_account_service import RememberedAccountService

    def broken(*_args, **_kwargs):
        raise OperationalError("INSERT", {}, Exception("BD ocupada"))

    monkeypatch.setattr(RememberedAccountService, "remember", broken)
    response = client.post(
        "/api/auth/login", json={"email": "admin@empresa.com", "password": "Admin1234", "remember": True}
    )
    assert response.status_code == 200 and response.json()["code"] == "LOGIN_SUCCESS"


# ---------------------------------------------------------------- reconocimiento facial


def test_unreadable_turn_capture_is_a_retryable_liveness_failure(client, company_headers):
    headers = approved_employee(client, company_headers)
    response = _verify(client, headers, turn="broken-turn:{person}")
    assert response.status_code == 200
    assert response.json()["data"]["verified"] is False  # no es un 500: se pide repetir la captura


def test_learning_failure_never_turns_a_success_into_an_error(client, company_headers, monkeypatch):
    from app.services.face_learning import FaceLearning

    def broken(*_args, **_kwargs):
        raise RuntimeError("falla inesperada al evaluar")

    monkeypatch.setattr(FaceLearning, "_candidate", broken)
    headers = approved_employee(client, company_headers)
    assert _verify(client, headers).json()["data"]["verified"] is True


def test_face_engine_loading_fails_fast_instead_of_blocking():
    from app.facial_recognition import FaceEngineUnavailable, _PipelineHolder

    holder = _PipelineHolder()
    holder._lock.acquire()  # otro hilo está cargando los modelos
    try:
        with pytest.raises(FaceEngineUnavailable):
            holder.pool()
    finally:
        holder._lock.release()


def test_waiting_for_a_face_worker_releases_the_database_connection():
    from app.dependencies import get_pipeline

    class Session:
        commits = 0

        def commit(self):
            Session.commits += 1

    dependency = get_pipeline(Session())  # type: ignore[arg-type]
    with pytest.raises(ServiceUnavailableError):  # sin modelos en las pruebas: 503, DESPUÉS de soltar la conexión
        next(dependency)
    assert Session.commits == 1


def test_gallery_cache_is_bounded_by_memory():
    import numpy as np

    from app.services.face_gallery import Gallery

    cache = FaceGalleryCache(max_companies=100, max_bytes=3000)
    gallery = Gallery((1, 1, ""), np.arange(2), np.arange(2), np.ones((2, 128), dtype=np.float32))  # ~1 KB
    for company in range(5):
        cache._store((company, "m"), gallery)
    kept = list(cache._items)
    assert kept[-1] == (4, "m") and sum(g.nbytes for g in cache._items.values()) <= 3000 and len(kept) < 5
    huge = Gallery((1, 1, ""), np.arange(9), np.arange(9), np.ones((9, 512), dtype=np.float32))  # > límite
    cache._store((9, "m"), huge)
    assert list(cache._items) == [(9, "m")]  # se pasa sola del límite, pero la recién usada se queda


# ---------------------------------------------------------------- base de datos y mantenimiento


def test_one_failing_purge_does_not_stop_the_others(monkeypatch):
    real = maintenance_service.delete_batch
    calls: list[str] = []

    def flaky(db, key, condition, limit):
        calls.append(key.class_.__name__)
        if len(calls) == 1:
            raise OperationalError("DELETE", {}, Exception("statement_timeout"))
        return real(db, key, condition, limit)

    monkeypatch.setattr(maintenance_service, "delete_batch", flaky)
    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db)
    assert len(removed) == len(maintenance_service.PURGES) and len(set(calls)) == len(maintenance_service.PURGES)


def test_catalogs_survive_a_database_blip(monkeypatch):
    first = catalog_service.get_catalogs()

    def down(_db):
        raise OperationalError("SELECT", {}, Exception("conexión rechazada"))

    monkeypatch.setattr(catalog_service, "load_catalogs", down)
    monkeypatch.setattr(settings, "CATALOG_CACHE_SECONDS", 0)
    assert catalog_service.get_catalogs() is first  # se sirven los anteriores


def _app_raising(error: Exception) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise error

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("sqlstate", "status", "code"),
    [("40P01", 409, "CONCURRENT_UPDATE"), ("57014", 503, "DATABASE_TIMEOUT"), (None, 503, "DATABASE_UNAVAILABLE")],
)
def test_database_errors_get_their_own_stable_code(sqlstate, status, code):
    class Origin(Exception):
        pass

    origin = Origin("pg")
    origin.sqlstate = sqlstate  # type: ignore[attr-defined]
    response = _app_raising(OperationalError("SELECT 1", {}, origin)).get("/boom")
    body = response.json()
    assert response.status_code == status and body["code"] == code and body["success"] is False
    assert set(body) >= {"statusCode", "message", "data", "errors", "traceId", "timestamp"}


def test_chunked_uploads_cannot_exceed_the_size_limit():
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/upload")
    async def upload(request: Request) -> dict:
        return {"size": len(await request.body())}

    @app.post("/form")
    async def form(name: Annotated[str, Form()]) -> dict:
        return {"name": name}

    @app.post("/json")
    async def as_json(payload: dict[str, str]) -> dict:
        return payload

    client = TestClient(SecurityMiddleware(app, max_body=10), raise_server_exceptions=False)

    def chunks():  # sin Content-Length: por partes
        return (part for part in (b"12345", b"67890", b"abcde"))

    # Igual si el cuerpo lo lee el código o FastAPI (formulario o JSON): 413 en español, no un 400.
    for path, content_type in (("/upload", None), ("/form", "application/x-www-form-urlencoded"), ("/json", None)):
        headers = {"Content-Type": content_type} if content_type else {"Content-Type": "application/json"}
        response = client.post(path, content=chunks(), headers=headers)
        body = response.json()
        assert response.status_code == 413 and body["code"] == "PAYLOAD_TOO_LARGE", path
        assert "excede el tamaño máximo" in body["message"]
    assert client.post("/upload", content=b"pequeno").json() == {"size": 7}
    # Un cuerpo ilegible tampoco responde en inglés.
    broken = client.post("/form", content=b"x", headers={"Content-Type": "multipart/form-data"})
    assert broken.status_code == 400 and broken.json()["message"] == "No se pudo leer el contenido de la solicitud"


def test_concurrent_manager_retries_are_idempotent(client, company_headers, monkeypatch):
    from app.repositories.department_repository import DepartmentRepository

    department = client.post("/api/departments", json={"name": "Producción"}, headers=company_headers).json()["data"]
    person = create_employee(client, company_headers).json()["data"]
    url = f"/api/departments/{department['id']}/managers"
    assert client.post(url, json={"employee_id": person["id"]}, headers=company_headers).status_code == 200
    # El reintento simultáneo no vio la fila (aún sin confirmar) e intenta insertarla otra vez.
    monkeypatch.setattr(DepartmentRepository, "is_manager", lambda *_: False)
    again = client.post(url, json={"employee_id": person["id"]}, headers=company_headers)
    assert again.status_code == 200 and [m["employee_id"] for m in again.json()["data"]["managers"]] == [person["id"]]


# ---------------------------------------------------------------- canal en vivo


def test_live_validation_survives_a_database_blip(client, company_headers, monkeypatch):
    from app.routers import realtime

    token = company_headers["Authorization"].removeprefix("Bearer ")

    def down(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("BD reiniciándose"))

    with client.websocket_connect("/api/ws/validation") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        assert ws.receive_json()["code"] == "WS_AUTHENTICATED"
        monkeypatch.setattr(realtime, "_validate", down)
        ws.send_text(json.dumps({"type": "validate", "field": "email", "value": "x@y.com", "id": "a1"}))
        failed = ws.receive_json()
        assert failed["code"] == "DATABASE_UNAVAILABLE" and failed["statusCode"] == 503
        ws.send_text(json.dumps({"type": "ping"}))  # el canal sigue abierto
        assert ws.receive_json()["code"] == "PONG"
